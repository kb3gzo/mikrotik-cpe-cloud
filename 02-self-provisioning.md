# Mikrotik CPE Cloud — Zero-Touch Self-Provisioning

_Draft 1 · 2026-04-21 · companion to `01-design-wireguard-and-telemetry.md`_

Bradford's new stock of routers is already passing through a factory pre-provisioning script that sets default IP, wifi, and identity on V7 firmware. This doc adds **one more block** to that script — after which, every new router that gets plugged into an internet connection enrolls itself, shows up in the admin UI, and starts pushing telemetry. No paste, no QR code, no field-tech action on the router.

The manual flow in §4.6 of the Phase 1 design is kept for retrofit scenarios (existing fleet, RMA returns, routers that arrive without pre-provisioning).

---

## 1. Flow overview

```
┌─────────────────────────────┐         ┌────────────────────────────────┐
│ Office / warehouse          │         │ mcc.bradfordbroadband.com      │
│                             │         │                                │
│ 1. Mint short-lived admin   │         │                                │
│    fetch token (CLI or UI)  │         │                                │
│ 2. Run factory prep script  │         │                                │
│    on new router:           │         │                                │
│    • default IP / wifi /    │         │                                │
│      identity               │         │                                │
│    • GET /factory/          │  HTTPS  │ 3. Validate fetch token        │
│      self-enroll.rsc        ├────────►│ 4. Embed current provisioning  │
│      (with fetch token)     │         │    secret into Jinja-rendered  │
│                             │◄────────┤    installer                   │
│    • /import installer.rsc  │         │                                │
│    • /file remove           │         │                                │
│                             │         │                                │
└──────────────┬──────────────┘         │                                │
               │ (router ships to       │                                │
               │  customer site)        │                                │
               ▼                        │                                │
┌─────────────────────────────┐         │                                │
│ Customer site — first boot  │         │                                │
│                             │         │                                │
│ 5. Router boots, gets DHCP  │         │                                │
│ 6. Startup scheduler fires  │         │                                │
│    cpe-cloud-enroll script  │         │                                │
│ 7. Script waits for DNS to  │         │                                │
│    resolve mcc.broadband    │         │                                │
│ 8. Creates wg-cpe-cloud     │         │                                │
│    interface (generates     │         │                                │
│    its own keypair)         │         │                                │
│ 9. POST enrollment payload  │  HTTPS  │                                │
│    (serial, mac, model,     ├────────►│ 10. Validate provisioning secret│
│    identity, pubkey;        │         │ 11. Match serial in DB:        │
│    X-Provisioning-Secret    │         │     • new     → INSERT pending │
│    header)                  │         │     • existing → UPDATE in place│
│                             │         │ 12. Allocate overlay IP        │
│                             │         │ 13. Mint telemetry token       │
│                             │         │ 14. Append peer to wg0.conf,   │
│                             │         │     wg syncconf                │
│                             │         │ 15. Render RouterOS provision  │
│                             │         │     script from template       │
│ 16. Receive .rsc response   │◄────────┤                                │
│ 17. /import the script:     │         │                                │
│     • apply overlay IP      │         │                                │
│     • add WG peer           │         │                                │
│     • install telemetry     │         │                                │
│       script + scheduler    │         │                                │
│     • create flag file      │         │                                │
│ 18. First telemetry push    │  HTTPS  │ 19. Admin UI shows router in   │
│     within 5 min            ├────────►│     pending queue (or auto-    │
│                             │         │     approved per rules)        │
└─────────────────────────────┘         └────────────────────────────────┘
```

Total wall-clock time from first boot to visible-in-UI is typically under 2 minutes, dominated by the DHCP → DNS-up waiting period. The first telemetry push lands within 5 minutes.

---

## 2. Security model

### 2.1 Threat model

- **Attacker on the public internet** tries to enroll a rogue device: blocked by the provisioning secret. They don't have it.
- **Attacker who sniffs an enrollment request** and replays it: blocked by TLS. Certificate validation is enforced server-side (Let's Encrypt) and by RouterOS's default cert validation (we don't disable it).
- **Attacker with physical access to a stock-unit router** (stolen from warehouse): can extract the provisioning secret from the script. They can then enroll arbitrary devices. Mitigated by: (a) pending-state default (router doesn't enter the active fleet until admin approves); (b) rate limiting; (c) admin alerting on unusual enrollment activity; (d) periodic secret rotation.
- **Attacker with physical access to an already-deployed router**: can read the telemetry token. Blast radius is one router's worth of data push — can't enroll new devices with it, can't read other routers' data. Token can be rotated from the server side over the WG tunnel.

The provisioning secret is **not** a strong auth primitive. It's a speed bump that funnels bad traffic into a quarantine queue rather than letting it through silently.

### 2.2 Secret rotation

The server accepts **up to two provisioning secrets at any time**: `current` and `previous`. Stored in a `provisioning_secrets` table with `valid_from` / `valid_until` timestamps. Rotation procedure:

1. Admin generates new secret, marks it `current`, demotes the old `current` to `previous` with a 60-day `valid_until`.
2. Factory pre-provisioning script is updated with the new secret. All new pre-provisioned routers use it.
3. Routers pre-provisioned before rotation but not yet enrolled (sitting on a shelf) can still enroll for 60 days using the `previous` secret.
4. After the grace window, the `previous` secret's row is deleted.

If a leak is detected, rotation can be immediate (no grace window), at the cost of invalidating any shelf stock — which would need to be re-prepped.

### 2.3 Pending-state approval

On successful enrollment, the server inserts the row with `status='pending'` **unless** an auto-approve rule matches. Auto-approve rules live in a `provisioning_rules` table and are evaluated in order:

| Rule                                      | Effect                            |
|-------------------------------------------|-----------------------------------|
| `identity LIKE 'hAP % - %, %'` (matches the naming convention) | `status='active'`, log audit event |
| `serial IN (admin-pre-registered list)`   | `status='active'`, log audit event |
| (fallback — no rule matches)              | `status='pending'`, notify admin   |

In Phase 1 we'll ship just the identity-pattern rule. If it matches the convention, we trust the on-site tech has set it correctly. Pre-registered serials is a Phase 2 refinement for RMAs and batch shipments.

### 2.4 Rate limiting

On the `/api/v1/auto-enroll` endpoint:
- 10 requests / minute / source IP
- 3 requests / minute / serial (across all source IPs)
- Alert the admin (Slack/email — TBD in Phase 2) on:
  - Any request with an invalid secret
  - More than 5 failed-validation requests in 10 minutes from any source
  - Any enrollment for a serial that has *already been decommissioned*

---

## 3. Factory pre-provisioning — the addition

Bradford's factory-prep script gains one additional block. Rather than inlining the whole self-enrollment script (which forces double-escaping of every nested quote and dollar sign), the block **downloads a rendered installer from the central server** and imports it. The installer itself is a flat, normal-looking RouterOS script with no escape gymnastics.

Two secrets are involved, with very different scopes:

| Secret                   | Lifetime    | Where it lives                                                       | What it authorizes                                                   |
|--------------------------|-------------|----------------------------------------------------------------------|----------------------------------------------------------------------|
| **Admin fetch token**    | Short (hours-to-days, per admin session) | Typed into the factory prep script at prep time, never stored on routers | Downloading the current installer from `/factory/self-enroll.rsc`    |
| **Provisioning secret**  | Long (quarterly rotation) | Embedded by the server into the rendered installer; lands in `cpe-cloud-enroll` script on the router | Calling `POST /api/v1/auto-enroll` during first boot                  |

This separation matters: a leaked fetch token lets an attacker download the current installer, but they still can't use it to enroll rogue devices (installers get baked into a device via `/import`, they don't directly authenticate to `/auto-enroll`). A leaked provisioning secret lets an attacker enroll rogue devices, but those land in `pending` status per §2.3.

### 3.1 The bootstrap block — what goes in the factory-prep script

This is short enough to paste in full. It's the *entire* addition to Bradford's existing factory-prep script, drop it in after the identity / IP / wifi configuration:

```routeros
# ─────────────────────────────────────────────────────────────────────────────
# CPE Cloud self-enrollment bootstrap
# Pastes the current short-lived admin fetch token, downloads the rendered
# installer from the central server, imports it, cleans up.
# Idempotent — safe to re-run.
# ─────────────────────────────────────────────────────────────────────────────

:local adminFetchToken "REPLACE_WITH_CURRENT_FETCH_TOKEN"

# Remove any previous version of the enrollment script/scheduler
:do { /system scheduler remove [find name=cpe-cloud-enroll] } on-error={}
:do { /system script    remove [find name=cpe-cloud-enroll] } on-error={}

# Download the installer (server embeds the current provisioning secret into it)
/tool fetch \
    url=("https://mcc.bradfordbroadband.com/factory/self-enroll.rsc?t=" . $adminFetchToken) \
    mode=https \
    dst-path=cpe-cloud-install.rsc \
    output=file

# Import the installer — it adds the cpe-cloud-enroll script + scheduler
/import cpe-cloud-install.rsc

# Clean up
/file remove cpe-cloud-install.rsc

:log info "cpe-cloud: factory-prep bootstrap complete"
```

That's the whole thing. If you'd rather not rely on the server being reachable at factory-prep time (for instance, you prep routers on a LAN with no internet until they ship), see Appendix A for a fully-inlined version that bakes the enrollment script directly into the factory-prep script without needing a hosted installer.

### 3.2 The installer endpoint — `GET /factory/self-enroll.rsc`

The server exposes this endpoint to return a freshly-rendered installer script. Every request gets the current provisioning secret embedded server-side, so rotations propagate without touching the factory-prep script.

**Request:**

```
GET /factory/self-enroll.rsc?t=<admin-fetch-token> HTTP/1.1
Host: mcc.bradfordbroadband.com
```

**Success response:**

```
HTTP/1.1 200 OK
Content-Type: text/plain; charset=utf-8

# ... rendered installer script, see §3.3 ...
```

**Failure responses** use the same pattern as `/auto-enroll`: return an RSC script that logs an error, with an appropriate HTTP status code for server-side monitoring. This way the factory-prep operator sees a readable `/log print` entry instead of cryptic fetch errors.

```routeros
:log error "cpe-cloud factory-prep rejected: fetch token expired or invalid"
```

Target Python path: `app/routers/factory.py`. Skeleton:

```python
"""Factory pre-provisioning installer endpoint."""
from __future__ import annotations

import logging
import secrets
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Query, Request, Response, status
from jinja2 import Environment, FileSystemLoader
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models import AdminFetchToken, ProvisioningSecret
from app.services.rate_limit import check_fetch_rate_limit
from app.services.tokens import hash_token

log = logging.getLogger(__name__)
router = APIRouter(prefix="/factory", tags=["factory"])

jinja_env = Environment(
    loader=FileSystemLoader("app/templates/rsc"),
    keep_trailing_newline=True,
)


def _rsc_error(message: str) -> str:
    safe = message.replace('"', "'")
    return f':log error "cpe-cloud factory-prep rejected: {safe}"\n'


@router.get("/self-enroll.rsc", response_class=Response)
async def factory_installer(
    request: Request,
    t: str = Query(..., min_length=16, max_length=128),
    session: AsyncSession = get_session(),  # real scaffold uses Depends()
) -> Response:
    source_ip = request.client.host if request.client else "unknown"

    # Rate limit: downloads should be sparse (one per device prepped)
    if not await check_fetch_rate_limit(source_ip):
        return Response(
            content=_rsc_error("rate limit exceeded"),
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            media_type="text/plain",
        )

    # Validate fetch token
    token_row = await _find_valid_fetch_token(session, t)
    if token_row is None:
        log.warning("factory-prep bad fetch token from %s", source_ip)
        return Response(
            content=_rsc_error("fetch token expired or invalid"),
            status_code=status.HTTP_401_UNAUTHORIZED,
            media_type="text/plain",
        )

    # Look up the current provisioning secret (plaintext version cached in
    # secret manager at rotation time — we never store plaintext in DB)
    current_secret = await _current_provisioning_secret_plaintext(session)
    if current_secret is None:
        log.error("factory-prep: no current provisioning secret configured")
        return Response(
            content=_rsc_error("server has no active provisioning secret"),
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            media_type="text/plain",
        )

    # Record the fetch for audit
    token_row.last_used_at = datetime.now(timezone.utc)
    token_row.use_count += 1
    await session.commit()

    # Render installer
    template = jinja_env.get_template("factory-installer.rsc.j2")
    rsc = template.render(
        provisioning_secret=current_secret,
        enrollment_url="https://mcc.bradfordbroadband.com/api/v1/auto-enroll",
        server_fqdn="mcc.bradfordbroadband.com",
        generated_at=datetime.now(timezone.utc).isoformat(),
        token_label=token_row.label,
    )
    return Response(content=rsc, media_type="text/plain")


async def _find_valid_fetch_token(session, raw: str):
    now = datetime.now(timezone.utc)
    candidates = await session.scalars(
        select(AdminFetchToken).where(
            AdminFetchToken.expires_at > now,
            AdminFetchToken.revoked_at.is_(None),
        )
    )
    for row in candidates:
        if secrets.compare_digest(row.token_hash, hash_token(raw)):
            return row
    return None


async def _current_provisioning_secret_plaintext(session) -> str | None:
    """Returns the current provisioning secret in plaintext.

    Plaintext secrets live in a separate secret store (e.g. systemd creds,
    HashiCorp Vault, or a local encrypted file) — NOT in Postgres. Postgres
    only has the hash for verification on /auto-enroll. This function is the
    integration point with whichever store you end up using.
    """
    # Implementation stub — replace with real secret-store lookup
    ...
```

**Important subtlety:** the `/auto-enroll` endpoint (§4) only ever sees the hash of the provisioning secret, but this endpoint (`/factory/self-enroll.rsc`) has to embed the *plaintext* secret into the installer it returns. That means the server needs access to the plaintext somewhere. Options:

- systemd `LoadCredential` / creds directory (simple, good default for a single DO droplet)
- Encrypted file read on startup with a key loaded from env
- HashiCorp Vault or similar (overkill for one droplet but Phase 2 material if you expand)

Either way, the plaintext secret is **not** stored in Postgres. Postgres only keeps hashes, used by `/auto-enroll` to verify requests.

### 3.3 The installer script template — `app/templates/rsc/factory-installer.rsc.j2`

This is the flat, escape-free version of what used to be the inner script. It reads top-to-bottom like a normal RouterOS script:

```routeros
# ─────────────────────────────────────────────────────────────────────────────
# CPE Cloud self-enrollment installer
# Generated at {{ generated_at }} for fetch-token "{{ token_label }}"
# This script is rendered per-request by the central server and is SAFE to
# inspect — the provisioning secret embedded below is the CURRENT one for
# the window this installer was downloaded in.
# ─────────────────────────────────────────────────────────────────────────────

# Remove any previous version so this is idempotent on re-prep
:do { /system scheduler remove [find name=cpe-cloud-enroll] } on-error={}
:do { /system script    remove [find name=cpe-cloud-enroll] } on-error={}

# Install the self-enrollment script. Uses source={ ... } brace syntax so the
# inner script body can use normal quotes and $ without escaping.
/system script add name=cpe-cloud-enroll \
    policy=read,write,policy,test,password,sensitive,romon \
    comment="CPE Cloud self-enrollment (installed {{ generated_at }})" \
    source={
        :local provisioningSecret "{{ provisioning_secret }}"
        :local enrollmentUrl      "{{ enrollment_url }}"
        :local flagFile           "cpe-cloud-enrolled.flag"

        # Already enrolled? Exit immediately.
        :if ([:len [/file find name=$flagFile]] > 0) do={ :return }

        # Wait for DNS / internet (up to 10 minutes, probing every 10s)
        :local ready false
        :local attempts 0
        :while ($ready = false && $attempts < 60) do={
            :do {
                :resolve {{ server_fqdn }}
                :set ready true
            } on-error={
                :delay 10s
                :set attempts ($attempts + 1)
            }
        }
        :if ($ready = false) do={
            :log warning "cpe-cloud: network not ready, will retry on next schedule"
            :return
        }

        # Ensure WireGuard interface exists (generates keypair on first add)
        :if ([:len [/interface wireguard find name=wg-cpe-cloud]] = 0) do={
            /interface wireguard add name=wg-cpe-cloud listen-port=13231 mtu=1420 disabled=no
            :delay 2s
        }

        :local myPubkey [/interface wireguard get [find name=wg-cpe-cloud] public-key]
        :local serial   ""
        :do { :set serial   [/system routerboard get serial-number] } on-error={}
        :local model    ""
        :do { :set model    [/system routerboard get model] } on-error={}
        :local identity [/system identity get name]
        :local rosVer   [/system resource get version]
        :local mac      [/interface ethernet get [find name=ether1] mac-address]

        # Detect wifi stack (ax² / ax³ have /interface wifi; older models don't)
        :local wifiStack "wireless"
        :do { [/interface wifi find]; :set wifiStack "wifi" } on-error={}

        # Build JSON payload
        :local payload "{"
        :set payload ($payload . "\"serial\":\"$serial\",")
        :set payload ($payload . "\"mac\":\"$mac\",")
        :set payload ($payload . "\"model\":\"$model\",")
        :set payload ($payload . "\"identity\":\"$identity\",")
        :set payload ($payload . "\"ros_version\":\"$rosVer\",")
        :set payload ($payload . "\"wifi_stack\":\"$wifiStack\",")
        :set payload ($payload . "\"router_public_key\":\"$myPubkey\"")
        :set payload ($payload . "}")

        # POST enrollment; server returns RSC script that finishes setup
        :do {
            /tool fetch url=$enrollmentUrl \
                http-method=post \
                http-header-field=("Content-Type: application/json,X-Provisioning-Secret: " . $provisioningSecret) \
                http-data=$payload \
                mode=https \
                dst-path=cpe-cloud-provision.rsc \
                output=file \
                keep-result=yes
        } on-error={
            :log warning "cpe-cloud: enrollment request failed, will retry on next schedule"
            :return
        }

        # Import returned provision script
        :do {
            /import cpe-cloud-provision.rsc
        } on-error={
            :log error "cpe-cloud: import of server response failed"
            :do { /file remove cpe-cloud-provision.rsc } on-error={}
            :return
        }

        :do { /file remove cpe-cloud-provision.rsc } on-error={}
        :log info "cpe-cloud: enrollment complete"
    }

# Schedule: run at boot, retry every 10 minutes until enrolled
/system scheduler add name=cpe-cloud-enroll \
    start-time=startup \
    interval=10m \
    on-event="/system script run cpe-cloud-enroll" \
    policy=read,write,policy,test,password,sensitive,romon \
    comment="CPE Cloud self-enrollment retry loop"

# Kick it off immediately — harmless if no internet yet (script exits early)
:do { /system script run cpe-cloud-enroll } on-error={}
```

Two things that make this work without escaping:

- **`source={ ... }` brace syntax.** RouterOS accepts either `source="..."` (string literal, escape hell) or `source={ ... }` (brace block, no escaping). The brace form preserves the body verbatim, treating it as RouterOS source code rather than a quoted string. The only constraint is that braces inside the body must balance — which they naturally do for well-formed RouterOS code.
- **String concatenation for building the `http-header-field`.** Instead of embedding `$provisioningSecret` inside a `"..."` literal (which would have no issues, but demonstrates a cleaner pattern), we use `("prefix " . $var)`. This is also how the installer's `/tool fetch url=(...)` call is built in §3.1.

### 3.4 Admin fetch token — lifecycle

The short-lived token that authorizes `/factory/self-enroll.rsc` downloads. Kept separate from session cookies so tooling (scripts, copies of the factory-prep text) never needs to carry a logged-in session.

Schema addition (append to the Postgres migrations):

```sql
CREATE TABLE admin_fetch_tokens (
    id              BIGSERIAL PRIMARY KEY,
    token_hash      TEXT NOT NULL UNIQUE,     -- sha256 of the token
    label           TEXT NOT NULL,            -- admin-chosen label e.g. "bench-2"
    issued_to       TEXT NOT NULL,            -- admin username
    expires_at      TIMESTAMPTZ NOT NULL,
    revoked_at      TIMESTAMPTZ,
    last_used_at    TIMESTAMPTZ,
    use_count       INTEGER NOT NULL DEFAULT 0,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX idx_admin_fetch_tokens_expires
    ON admin_fetch_tokens(expires_at)
    WHERE revoked_at IS NULL;
```

Mint/revoke from the ops CLI:

```bash
# Mint a fetch token — default 7 day lifetime
python -m app.cli fetch-token mint \
    --label "bench-2" --issued-to aaron --valid-days 7
# Outputs:
#   Token: ft_xyz123...  (paste into factory-prep script)
#   Expires: 2026-04-28T14:00:00Z

# Revoke if leaked or tech leaves
python -m app.cli fetch-token revoke --label "bench-2"

# Audit: see who minted what and how many times it's been used
python -m app.cli fetch-token list
```

Because a fetch token alone can't enroll devices (it only downloads the installer), leakage is low-severity. It becomes material only when combined with physical-access enrollment of a rogue router onto a Bradford LAN, in which case the pending-status check in §2.3 still quarantines the result.

---

## 4. Server endpoint: `POST /api/v1/auto-enroll`

### 4.1 Request

```
POST /api/v1/auto-enroll HTTP/1.1
Host: mcc.bradfordbroadband.com
Content-Type: application/json
X-Provisioning-Secret: <current-or-previous-secret>

{
  "serial": "HC1234567A",
  "mac": "CC:2D:E0:12:34:56",
  "model": "hAP ac lite",
  "identity": "hAP ac lite - Smith, John",
  "ros_version": "7.14.2",
  "wifi_stack": "wireless",
  "router_public_key": "xTIBA9rboUdnM3HNyLwxcOhVmUiDHvjvrE1nMAIv+XI="
}
```

### 4.2 Response on success

```
HTTP/1.1 200 OK
Content-Type: text/plain; charset=utf-8

# RouterOS provisioning script (see §5 for the template)
...
```

We return the response as `text/plain` containing RouterOS script source. RouterOS doesn't care about content types for `/import`; it cares about the file extension, which we control via `dst-path=cpe-cloud-provision.rsc`.

### 4.3 Response on failure

Return **an RSC script that logs an error and exits** — not a JSON error body. This is deliberate: RouterOS can't parse JSON natively, and if we return 4xx the `/tool fetch` call errors out with a generic message that's hard to debug from the field. An RSC-formatted error lets the router log something human-readable.

```routeros
# Server-generated failure response
:log error "cpe-cloud enrollment rejected: invalid provisioning secret"
```

The `/import` will succeed (because an RSC with just a `:log` call is valid) and the router will log the reason. The admin gets the real error on the server side via structured logging.

The HTTP status code is set appropriately (401, 429, 500) for monitoring/alerting purposes, even though the router itself ignores it.

### 4.4 Python handler (FastAPI)

Target path: `app/routers/auto_enroll.py` in the scaffold.

```python
"""Auto-enrollment endpoint for zero-touch provisioning."""
from __future__ import annotations

import ipaddress
import logging
import secrets
from datetime import datetime, timezone

from fastapi import APIRouter, Header, HTTPException, Request, Response, status
from jinja2 import Environment, FileSystemLoader, select_autoescape
from pydantic import BaseModel, Field, constr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models import (
    AuditLog,
    ProvisioningSecret,
    Router,
    RouterToken,
)
from app.services import wireguard as wg_service
from app.services.rate_limit import check_rate_limit
from app.services.tokens import hash_token, mint_telemetry_token

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1", tags=["auto-enroll"])

jinja_env = Environment(
    loader=FileSystemLoader("app/templates/rsc"),
    autoescape=select_autoescape(disabled_extensions=("rsc",)),
    keep_trailing_newline=True,
)


class AutoEnrollRequest(BaseModel):
    serial: constr(strip_whitespace=True, min_length=4, max_length=64)
    mac: constr(strip_whitespace=True, min_length=17, max_length=17)
    model: constr(strip_whitespace=True, min_length=1, max_length=64)
    identity: constr(strip_whitespace=True, min_length=1, max_length=128)
    ros_version: constr(strip_whitespace=True, min_length=1, max_length=32)
    wifi_stack: str = Field(pattern=r"^(wireless|wifi)$")
    router_public_key: constr(strip_whitespace=True, min_length=43, max_length=44)


def _rsc_error(message: str) -> str:
    """Return an RSC script that logs the given error and exits."""
    safe = message.replace('"', "'")
    return f':log error "cpe-cloud enrollment rejected: {safe}"\n'


@router.post(
    "/auto-enroll",
    status_code=status.HTTP_200_OK,
    response_class=Response,
    responses={
        200: {"content": {"text/plain": {}}},
        401: {"content": {"text/plain": {}}},
        429: {"content": {"text/plain": {}}},
    },
)
async def auto_enroll(
    request: Request,
    payload: AutoEnrollRequest,
    x_provisioning_secret: str | None = Header(default=None),
    session: AsyncSession = get_session(),
) -> Response:
    source_ip = request.client.host if request.client else "unknown"

    # --- Rate limiting ------------------------------------------------------
    if not await check_rate_limit(source_ip, payload.serial):
        log.warning("auto-enroll rate limited ip=%s serial=%s", source_ip, payload.serial)
        return Response(
            content=_rsc_error("rate limit exceeded, try again later"),
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            media_type="text/plain",
        )

    # --- Secret validation --------------------------------------------------
    if not x_provisioning_secret:
        return Response(
            content=_rsc_error("missing provisioning secret header"),
            status_code=status.HTTP_401_UNAUTHORIZED,
            media_type="text/plain",
        )

    secret_row = await _find_valid_secret(session, x_provisioning_secret)
    if secret_row is None:
        log.warning(
            "auto-enroll bad secret ip=%s serial=%s identity=%r",
            source_ip, payload.serial, payload.identity,
        )
        await _audit(session, actor="auto-enroll", action="reject_bad_secret",
                     params={"source_ip": source_ip, "serial": payload.serial})
        return Response(
            content=_rsc_error("invalid provisioning secret"),
            status_code=status.HTTP_401_UNAUTHORIZED,
            media_type="text/plain",
        )

    # --- Find or create router row -----------------------------------------
    existing = await session.scalar(
        select(Router).where(Router.serial_number == payload.serial)
    )

    if existing is None:
        overlay_ip = await wg_service.allocate_overlay_ip(session)
        router_row = Router(
            identity=payload.identity,
            serial_number=payload.serial,
            mac_address=payload.mac,
            model=payload.model,
            ros_version=payload.ros_version,
            ros_major=_parse_ros_major(payload.ros_version),
            wifi_stack=payload.wifi_stack,
            wg_public_key=payload.router_public_key,
            wg_overlay_ip=overlay_ip,
            enrolled_at=datetime.now(timezone.utc),
            status=_initial_status_for(payload.identity),
        )
        session.add(router_row)
        await session.flush()
        action = "auto_enroll_new"
    else:
        # Re-enrollment: factory reset or swapped keypair.
        # Keep the existing overlay IP, rotate keys/tokens.
        existing.identity = payload.identity
        existing.mac_address = payload.mac
        existing.model = payload.model
        existing.ros_version = payload.ros_version
        existing.ros_major = _parse_ros_major(payload.ros_version)
        existing.wifi_stack = payload.wifi_stack
        existing.wg_public_key = payload.router_public_key
        existing.enrolled_at = datetime.now(timezone.utc)
        # Revoke old tokens; we'll mint a fresh one below.
        for tok in existing.tokens:
            if tok.revoked_at is None:
                tok.revoked_at = datetime.now(timezone.utc)
        router_row = existing
        action = "auto_enroll_reenroll"

    # --- Mint telemetry token ----------------------------------------------
    raw_token = mint_telemetry_token()
    session.add(RouterToken(
        router_id=router_row.id,
        token_hash=hash_token(raw_token),
        token_prefix=raw_token[:8],
    ))

    # --- Sync wg0 conf ------------------------------------------------------
    await session.commit()
    await wg_service.sync_from_db()

    await _audit(
        session,
        actor="auto-enroll",
        action=action,
        params={
            "source_ip": source_ip,
            "serial": payload.serial,
            "identity": payload.identity,
            "status": router_row.status,
            "secret_id": secret_row.id,
        },
        router_id=router_row.id,
    )
    await session.commit()

    # --- Render provisioning RSC -------------------------------------------
    template = jinja_env.get_template("provision.rsc.j2")
    rsc = template.render(
        router=router_row,
        telemetry_token=raw_token,
        server_public_key=await wg_service.get_server_public_key(),
        server_endpoint="mcc.bradfordbroadband.com",
        server_port=51820,
        overlay_cidr="10.100.0.0/22",
        telemetry_url="https://mcc.bradfordbroadband.com/api/v1/telemetry",
    )
    return Response(content=rsc, media_type="text/plain")


def _parse_ros_major(version: str) -> int:
    try:
        return int(version.split(".")[0])
    except (ValueError, IndexError):
        return 0


def _initial_status_for(identity: str) -> str:
    """Phase 1 auto-approve rule: identity matching the naming convention → active."""
    import re
    if re.match(r"^hAP .+ - .+, .+$", identity):
        return "active"
    return "pending"


async def _find_valid_secret(session: AsyncSession, raw: str) -> ProvisioningSecret | None:
    now = datetime.now(timezone.utc)
    candidates = await session.scalars(
        select(ProvisioningSecret).where(
            ProvisioningSecret.valid_from <= now,
            ProvisioningSecret.valid_until > now,
        )
    )
    for row in candidates:
        # Constant-time comparison of hashes
        if secrets.compare_digest(row.secret_hash, hash_token(raw)):
            return row
    return None


async def _audit(session: AsyncSession, **kwargs) -> None:
    session.add(AuditLog(
        actor=kwargs.pop("actor"),
        action=kwargs.pop("action"),
        status="success",
        router_id=kwargs.pop("router_id", None),
        params=kwargs.pop("params", {}),
    ))
```

A few notes on this handler:

- `Depends` is left off `get_session` in this snippet for brevity; the actual scaffold will wire it via FastAPI's dependency injection.
- `mint_telemetry_token()` produces a URL-safe 48-byte random string. The raw value is returned to the router *once* (in the generated RSC); only the hash is stored in Postgres.
- `wg_service.sync_from_db()` is the one operation that needs root. It's implemented as a shell-out to a small sudo-wrapped helper, per §8 of the main design doc.
- Re-enrollment keeps the existing overlay IP (line: `# Keep the existing overlay IP, rotate keys/tokens.`). This matters because if a router did a factory reset at a customer's house, we don't want to cycle its IP — the admin's bookmarks and monitoring alerts keep working.
- The `_initial_status_for` function is the Phase 1 auto-approve rule. When Phase 2 adds pre-registered serials, this function expands to check that table too.

### 4.5 SQL additions for this endpoint

Append to the schema in §7 of the main design doc:

```sql
-- Provisioning secrets (current + previous during rotation grace periods)
CREATE TABLE provisioning_secrets (
    id              BIGSERIAL PRIMARY KEY,
    secret_hash     TEXT NOT NULL UNIQUE,           -- sha256 of the secret
    label           TEXT NOT NULL,                  -- human label e.g. "2026-Q2"
    valid_from      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    valid_until     TIMESTAMPTZ NOT NULL,           -- hard expiry
    created_by      TEXT NOT NULL,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX idx_provisioning_secrets_window
    ON provisioning_secrets(valid_from, valid_until);

-- Auto-approve rules (Phase 2 expansion point; Phase 1 can start empty)
CREATE TABLE provisioning_rules (
    id              BIGSERIAL PRIMARY KEY,
    kind            TEXT NOT NULL,                  -- 'identity_pattern' | 'serial_list'
    pattern         TEXT,                           -- regex for identity, or NULL
    serials         TEXT[],                         -- for serial_list rules
    priority        INTEGER DEFAULT 100,
    enabled         BOOLEAN DEFAULT true,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);
```

---

## 5. Server-rendered RouterOS provision template

File: `app/templates/rsc/provision.rsc.j2`. This is what gets sent back to the router and imported on enrollment.

```
# ════════════════════════════════════════════════════════════════════
# CPE Cloud provision — generated by server for router_id={{ router.id }}
# Identity: {{ router.identity }}
# Overlay IP: {{ router.wg_overlay_ip }}
# Generated at: {{ now() }}
# ════════════════════════════════════════════════════════════════════

# --- Apply overlay IP to WG interface --------------------------------
:do {
    /ip address remove [find interface=wg-cpe-cloud]
} on-error={}

/ip address add \
    address={{ router.wg_overlay_ip }}/32 \
    interface=wg-cpe-cloud \
    comment="cpe-cloud overlay"

# --- Add/replace peer pointing at the central server ----------------
:do {
    /interface wireguard peers remove [find interface=wg-cpe-cloud]
} on-error={}

/interface wireguard peers add \
    interface=wg-cpe-cloud \
    public-key="{{ server_public_key }}" \
    endpoint-address={{ server_endpoint }} \
    endpoint-port={{ server_port }} \
    allowed-address={{ overlay_cidr }} \
    persistent-keepalive=25s \
    comment="cpe-cloud server"

# --- Firewall: only allow input on wg-cpe-cloud from the server -----
:do {
    /ip firewall filter remove [find comment="cpe-cloud-wg-accept"]
} on-error={}

/ip firewall filter add \
    chain=input \
    in-interface=wg-cpe-cloud \
    src-address=10.100.0.1 \
    action=accept \
    place-before=0 \
    comment="cpe-cloud-wg-accept"

/ip firewall filter add \
    chain=input \
    in-interface=wg-cpe-cloud \
    action=drop \
    comment="cpe-cloud-wg-drop-other"

# --- Install telemetry script ---------------------------------------
:do {
    /system script remove [find name=cpe-cloud-telemetry]
} on-error={}

/system script add name=cpe-cloud-telemetry policy=read,test,policy source="
{%- if router.wifi_stack == 'wifi' %}
{% include 'telemetry-wifi.rsc.j2' %}
{%- else %}
{% include 'telemetry-wireless.rsc.j2' %}
{%- endif %}
"

# --- Schedule telemetry push ----------------------------------------
:do {
    /system scheduler remove [find name=cpe-cloud-telemetry]
} on-error={}

/system scheduler add \
    name=cpe-cloud-telemetry \
    interval=5m \
    start-time=startup \
    on-event="/system script run cpe-cloud-telemetry" \
    policy=read,test,policy \
    comment="CPE Cloud telemetry push"

# --- Mark enrollment complete ---------------------------------------
# The self-enroll script checks existence via :len [/file find name=...],
# so a zero-byte placeholder is fine. /file print is the standard RouterOS
# idiom for creating a marker file.
/file print file=cpe-cloud-enrolled.flag
:delay 500ms

:log info "cpe-cloud: provisioning applied, router_id={{ router.id }}, overlay_ip={{ router.wg_overlay_ip }}"
```

The `{% include 'telemetry-wireless.rsc.j2' %}` lines expand into the RouterOS scripts from §6.1 / §6.2 of the main design doc, with the `authToken` line replaced by the freshly-minted `{{ telemetry_token }}`. Keep those telemetry scripts as separate Jinja templates so the provision template stays readable.

The substring `"{{ telemetry_token }}"` gets embedded into the inlined telemetry script. All the inner double-quotes inside the `source="..."` block are the standard RouterOS script-in-script escaping — handled by the Jinja include directive, not by you.

---

## 6. Re-enrollment after factory reset

This is worth walking through explicitly because it's the most common "something went weird" scenario.

1. Router gets factory-reset at a customer's house (usually because the tech is troubleshooting).
2. Factory reset wipes everything including the WG keypair, the flag file, the telemetry script, the scheduler, and the self-enrollment script itself.
3. The factory-prep script has to be re-run (on a techs' laptop connected to the router, or via netinstall). This puts back identity, wifi, IP, and the self-enrollment machinery with the current provisioning secret.
4. On next boot, self-enrollment runs again. It POSTs fresh data with a **new public key**.
5. Server matches on `serial`, finds the existing router row, updates it: new pubkey, new telemetry token, **same overlay IP**.
6. Server appends the new pubkey + overlay IP to `wg0.conf` (replacing the old entry for the same IP). `wg syncconf` applies live without dropping other peers.
7. Router imports the new provision script, comes back online. The admin UI briefly shows `last_seen_at` as stale, then fresh again.

The one case this doesn't cleanly handle: factory-reset *without* re-running the prep script (e.g. router recovered from DFS, no self-enrollment script exists anymore). In that case the router is offline from our POV and needs a tech to re-prep. We'll see it in the admin UI as "last seen > 24 hours" and can trigger an alert.

---

## 7. Operations cheat sheet

### Generate a new provisioning secret
```bash
# On the server:
python -m app.cli provisioning-secret mint --label "2026-Q3" --valid-days 90
# Outputs:
#   Secret: xyz123... (copy this into the factory prep script)
#   Hash stored with id=5, valid until 2026-07-20
```

### Revoke the current secret immediately (emergency)
```bash
python -m app.cli provisioning-secret revoke --id 5
# Marks valid_until = NOW(), invalidates all in-flight enrollments using it.
```

### See pending routers awaiting approval
```bash
python -m app.cli routers list --status pending
# Or in the web UI: /admin/routers?status=pending
```

### Approve a pending router
```bash
python -m app.cli routers approve --id 142
# Or in the web UI: click "Approve" on the router detail page.
```

### Force re-enrollment for a misbehaving router (from admin side)
```bash
python -m app.cli routers reset-enrollment --id 142
# Deletes the row, drops the WG peer, issues an order to re-run factory prep.
# After re-prep, router will auto-enroll fresh.
```

### Mint an admin fetch token for a factory-prep session
```bash
python -m app.cli fetch-token mint \
    --label "bench-2" --issued-to aaron --valid-days 7
# Outputs:
#   Token: ft_xyz123...  (paste into factory-prep script)
#   Expires: 2026-04-28T14:00:00Z
```

### Revoke an admin fetch token
```bash
python -m app.cli fetch-token revoke --label "bench-2"
```

---

## 8. What's deferred to Phase 2+

- Alerting (Slack/email) on suspicious enrollment patterns
- Pre-registered serials auto-approve rule
- UISP client auto-link during enrollment (match `{LastName}, {FirstName}` against UISP client list)
- Per-batch provisioning secrets (one per shipping lot, with built-in expiry)
- TLS client cert enrollment (stronger auth than shared secret; requires cert lifecycle management)

---

## 9. Open questions / things I picked defaults on

1. **Retry interval of 10 minutes** — reasonable compromise between "enroll fast when internet comes up" and "don't hammer the server if it's down." Adjustable in the factory script.
2. **DNS wait of 10 minutes on boot** — enough for even slow cable modem handshakes. If customers have networks that take longer to come up, bump this.
3. **Identity-pattern auto-approve regex** — `^hAP .+ - .+, .+$`. Strict enough to avoid accepting the generic "MikroTik" default, loose enough to accept any model variant. If you have routers that don't begin with `hAP ` (hAP lte4 for instance, or future models), broaden the regex.
4. **Firewall rules** — the provision template adds `accept from 10.100.0.1` + `drop everything else` on the WG interface. This is safe-by-default but means if we ever want peer-to-peer WG between routers we'd need to update. Not planned.

None of these should block moving forward. Let me know if any of the defaults look wrong.

---

## Appendix A — Fallback: fully-inlined factory-prep script

**Use this version only if** the factory-prep workstation can't reach `mcc.bradfordbroadband.com` at prep time (e.g. you prep on an isolated LAN, or for offline kit preparation). It embeds the entire self-enrollment script inline, avoiding the need to download an installer from the server.

The tradeoff: every character inside the inner script is double-escaped. Maintenance is painful — any change has to be retested for escape correctness, and the failure mode when you get it wrong is "the router silently installs a syntactically broken script that errors on first enrollment attempt."

If you go with this version, bake the current provisioning secret directly into the `REPLACE_WITH_CURRENT_SECRET` placeholder before running on each batch, and rotate the entire factory-prep script (not just the secret) when the secret changes.

```routeros
# ─────────────────────────────────────────────────────────────────────────────
# CPE Cloud self-enrollment bootstrap — FALLBACK (fully-inlined)
# Use only when the factory-prep host can't reach the central server.
# See §3 for the recommended hosted-installer approach.
# ─────────────────────────────────────────────────────────────────────────────

:local provisioningSecret "REPLACE_WITH_CURRENT_SECRET"
:local enrollmentUrl      "https://mcc.bradfordbroadband.com/api/v1/auto-enroll"

# Remove any previous version of our script/scheduler
:do { /system scheduler remove [find name=cpe-cloud-enroll] } on-error={}
:do { /system script    remove [find name=cpe-cloud-enroll] } on-error={}

# Install the self-enrollment script (note the heavy escaping — see §3 walkthrough)
/system script add name=cpe-cloud-enroll policy=read,write,policy,test,password,sensitive,romon source="
:local provisioningSecret \"$provisioningSecret\"
:local enrollmentUrl      \"$enrollmentUrl\"
:local flagFile           \"cpe-cloud-enrolled.flag\"

:if ([:len [/file find name=\$flagFile]] > 0) do={ :return }

:local ready false
:local attempts 0
:while (\$ready = false && \$attempts < 60) do={
    :do { :resolve mcc.bradfordbroadband.com; :set ready true } on-error={
        :delay 10s
        :set attempts (\$attempts + 1)
    }
}
:if (\$ready = false) do={
    :log warning \"cpe-cloud: network not ready, will retry on next schedule\"
    :return
}

:if ([:len [/interface wireguard find name=wg-cpe-cloud]] = 0) do={
    /interface wireguard add name=wg-cpe-cloud listen-port=13231 mtu=1420 disabled=no
    :delay 2s
}

:local myPubkey [/interface wireguard get [find name=wg-cpe-cloud] public-key]
:local serial   \"\"
:do { :set serial   [/system routerboard get serial-number] } on-error={}
:local model    \"\"
:do { :set model    [/system routerboard get model] } on-error={}
:local identity [/system identity get name]
:local rosVer   [/system resource get version]
:local mac      [/interface ethernet get [find name=ether1] mac-address]

:local wifiStack \"wireless\"
:do { [/interface wifi find]; :set wifiStack \"wifi\" } on-error={}

:local payload \"{\"
:set payload (\$payload . \"\\\"serial\\\":\\\"\$serial\\\",\")
:set payload (\$payload . \"\\\"mac\\\":\\\"\$mac\\\",\")
:set payload (\$payload . \"\\\"model\\\":\\\"\$model\\\",\")
:set payload (\$payload . \"\\\"identity\\\":\\\"\$identity\\\",\")
:set payload (\$payload . \"\\\"ros_version\\\":\\\"\$rosVer\\\",\")
:set payload (\$payload . \"\\\"wifi_stack\\\":\\\"\$wifiStack\\\",\")
:set payload (\$payload . \"\\\"router_public_key\\\":\\\"\$myPubkey\\\"\")
:set payload (\$payload . \"}\")

:do {
    /tool fetch url=\$enrollmentUrl \\
        http-method=post \\
        http-header-field=\"Content-Type: application/json,X-Provisioning-Secret: \$provisioningSecret\" \\
        http-data=\$payload \\
        mode=https \\
        dst-path=cpe-cloud-provision.rsc \\
        output=file \\
        keep-result=yes
} on-error={
    :log warning \"cpe-cloud: enrollment request failed, will retry on next schedule\"
    :return
}

:do { /import cpe-cloud-provision.rsc } on-error={
    :log error \"cpe-cloud: import of server response failed\"
    :do { /file remove cpe-cloud-provision.rsc } on-error={}
    :return
}

:do { /file remove cpe-cloud-provision.rsc } on-error={}
:log info \"cpe-cloud: enrollment complete\"
"

/system scheduler add name=cpe-cloud-enroll \
    start-time=startup \
    interval=10m \
    on-event="/system script run cpe-cloud-enroll" \
    policy=read,write,policy,test,password,sensitive,romon \
    comment="CPE Cloud self-enrollment retry loop"

:do { /system script run cpe-cloud-enroll } on-error={}
```

**If you find yourself tempted to modify this version**, seriously consider standing up the hosted installer instead. The hosted path makes the script editable as normal RouterOS code and avoids the class of bugs that don't show up until a router is already shipped.
