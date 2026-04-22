# Mikrotik CPE Cloud — Phase 1 Design

**WireGuard Management Overlay + Telemetry Push Architecture**

_Draft 1 · 2026-04-21_

---

## 1. Goals of this phase

This phase covers **just the foundation**: getting every Mikrotik reachable over a secure management overlay, getting telemetry flowing from them into a central store, and having an inventory database that can be joined against UISP. Remote SSID/password changes, backup storage, and the UISP-integrated UI are explicitly out of scope for Phase 1 — they get built on top of what this phase produces.

Concrete success criteria:

1. Every Mikrotik router can establish an outbound WireGuard tunnel to the central server, receive a stable overlay IP, and survive WAN NAT / reboot.
2. Every Mikrotik pushes a JSON telemetry blob every 5 minutes containing system health, ethernet stats, wireless/wifi interface config, and the current client registration table.
3. The central server stores current-state inventory in Postgres and time-series telemetry in InfluxDB.
4. Each router row in Postgres can be joined to a UISP client/service by `uisp_client_id` so we can look up "which AirMax belongs with this Mikrotik" later.

Non-goals for Phase 1: a UI, alerting, SSID management, backups, V6→V7 migration automation, UISP plugin work.

---

## 2. High-level architecture

```
                                   ┌─────────────────────────────────────┐
                                   │       Central Server (Ubuntu 22)    │
                                   │       mcc.bradfordbroadband.com     │
  ┌─────────────┐                  │                                     │
  │ Mikrotik #1 ├──WAN──NAT──┐     │  :51820/udp  ──► wg0 (10.100.0.1)  │
  │   hAP ac²   │            │     │                  │                  │
  └─────────────┘            │     │                  ▼                  │
                             ├────►│  :443/tcp    ──► nginx (TLS)        │
  ┌─────────────┐            │     │                  │                  │
  │ Mikrotik #2 ├──WAN──NAT──┤     │                  ▼                  │
  │   hAP ax³   │            │     │            FastAPI (collector)      │
  └─────────────┘            │     │             │            │          │
                             │     │             ▼            ▼          │
  ┌─────────────┐            │     │          Postgres     InfluxDB      │
  │  ... ×700   ├────────────┘     │          (inventory)  (timeseries)  │
  └─────────────┘                  └─────────────────────────────────────┘

        ┌──── join on uisp_client_id ────┐
        ▼                                ▼
  ┌────────────────────┐          ┌───────────────────┐
  │  UISP (existing DO │          │  This system's    │
  │  droplet)          │          │  inventory        │
  │  authoritative for │          │  authoritative for│
  │  AirMax + clients  │          │  Mikrotiks        │
  └────────────────────┘          └───────────────────┘
```

Two network paths from each router to the central server:

- **Tunnel (WireGuard, UDP/51820):** always-up, used for server-initiated actions in later phases (SSID push, reboot, fetch backup).
- **HTTPS (TCP/443):** used for router-initiated telemetry pushes. Doesn't ride the tunnel — routers resolve the public hostname and post directly. This means telemetry keeps flowing even if the tunnel hiccups, and it simplifies debugging.

---

## 3. Central server layout

### 3.1 Host

- 1 VM on Digital Ocean, Ubuntu 22.04 LTS, minimum 4 GB RAM / 2 vCPU / 80 GB SSD to start. Will scale vertically long before we run out of room; 700 routers pushing 5-minute payloads of ~4 KB each is ~800 KB/min of ingress, trivial.
- Public hostname: `mcc.bradfordbroadband.com` with an A record to the droplet's public IP.
- Valid TLS certificate via Let's Encrypt / certbot. Self-signed won't work — RouterOS's `/tool fetch` validates certs by default and disabling that is a bad habit to build into 700 routers.

### 3.2 Services & ports

| Service       | Port       | Purpose                                        |
|---------------|------------|------------------------------------------------|
| WireGuard     | 51820/udp  | Management overlay                             |
| nginx         | 443/tcp    | TLS termination, reverse proxy for FastAPI     |
| FastAPI       | 127.0.0.1:8000 | Telemetry ingest, enrollment, future API   |
| Postgres 15   | 127.0.0.1:5432 | Inventory + audit log                      |
| InfluxDB 2.x  | 127.0.0.1:8086 | Telemetry time-series                      |
| SSH           | 22/tcp     | Admin only, key-only, firewalled to admin IPs  |

Postgres and Influx bind to localhost only. FastAPI is not exposed directly — only through nginx on 443.

### 3.3 Directory layout

```
/opt/cpe-cloud/
├── venv/                     # Python virtualenv
├── app/                      # FastAPI application
│   ├── main.py
│   ├── models.py             # SQLAlchemy models
│   ├── schemas.py            # Pydantic schemas
│   ├── routers/
│   │   ├── enrollment.py
│   │   └── telemetry.py
│   ├── services/
│   │   ├── wireguard.py      # generates peer configs, manages wg0
│   │   ├── influx.py         # writes telemetry to Influx
│   │   └── uisp.py           # UISP API client (Phase 2)
│   └── config.py
├── scripts/
│   ├── router-bootstrap.rsc  # template; one per new router
│   ├── wireless-telemetry.rsc
│   └── wifi-telemetry.rsc
└── .env                       # secrets (DB, Influx token, WG server key)

/etc/wireguard/wg0.conf        # managed by the wireguard service
/var/log/cpe-cloud/            # application logs
```

### 3.4 Why FastAPI + Postgres + InfluxDB

- FastAPI: great ergonomics for JSON APIs, native async, trivial to mount both the telemetry ingest and future control-plane endpoints in the same process.
- Postgres for inventory: rows that change once a day, relational joins against UISP ID, JSONB for payload metadata.
- InfluxDB for telemetry: 700 routers × 12 pushes/hour × many points per push = millions of points per day. Postgres can do it but Influx makes Grafana dashboards essentially free, and retention policies / downsampling are first-class. Use InfluxDB 2.x with Flux — the bucket + token model is clean.

---

## 4. WireGuard overlay design

### 4.1 Addressing

- Overlay subnet: **`10.100.0.0/22`** (1,022 usable hosts — comfortable headroom above 700).
- `10.100.0.1` = central server on `wg0`.
- `10.100.0.2` – `10.100.3.254` = routers, allocated sequentially from Postgres on enrollment.
- IP allocation is the server's responsibility, never the router's. The Postgres `routers` table is the source of truth; `wg0.conf` is regenerated from it.

Pick a subnet that doesn't collide with anything on the customer LAN side. `10.100.x.x` is arbitrary but uncommon; if your internal management network uses `10.0.0.0/8` already, switch to something like `172.31.128.0/22`.

### 4.2 Server wg0 configuration

The `[Interface]` stanza is static; `[Peer]` stanzas are regenerated from Postgres any time a router enrolls or is removed.

```ini
# /etc/wireguard/wg0.conf
[Interface]
Address = 10.100.0.1/22
ListenPort = 51820
PrivateKey = <SERVER_PRIVATE_KEY>
# Rebuild peers from DB after any enrollment:
PostUp = /opt/cpe-cloud/venv/bin/python -m app.services.wireguard sync

# [Peer] blocks appended by the sync tool
```

Peer blocks look like this (one per router):

```ini
[Peer]
# router_id=42 serial=HC1234567A identity=cust-smith-john
PublicKey = <ROUTER_PUBLIC_KEY>
AllowedIPs = 10.100.0.42/32
PersistentKeepalive = 0  # server side doesn't need to keepalive
```

### 4.3 Router-side WireGuard (RouterOS 7)

On the router the WG interface is created once at enrollment, then left alone:

```routeros
# Created at enrollment; values filled in by bootstrap script
/interface wireguard add name=wg-cpe-cloud listen-port=13231 mtu=1420
/ip address add address=10.100.0.42/32 interface=wg-cpe-cloud
/interface wireguard peers add \
    interface=wg-cpe-cloud \
    public-key="<SERVER_PUBLIC_KEY>" \
    endpoint-address=mcc.bradfordbroadband.com \
    endpoint-port=51820 \
    allowed-address=10.100.0.0/22 \
    persistent-keepalive=25s
```

Key points:

- The router generates its own keypair the instant you run `/interface wireguard add`. The private key **never leaves the router**. You read its public key with `/interface wireguard get [find name=wg-cpe-cloud] public-key` and send *that* to the server during enrollment.
- `persistent-keepalive=25s` on the router side is what keeps the NAT mapping alive. Essential — without it, WAN-side NAT table entries expire and the server can't initiate anything inbound.
- `allowed-address=10.100.0.0/22` on the router side means "route the whole management overlay through this tunnel." Do **not** set `0.0.0.0/0` — we only want management traffic in the tunnel, not customer traffic.
- Don't NAT the WG interface. Don't add it to any bridge. Leave it isolated.
- Firewall on the router should only accept input on the WG interface from `10.100.0.1/32`. Put that rule at the top of `/ip firewall filter`.

### 4.4 Router-side OpenVPN fallback (RouterOS 6)

RouterOS 6 can do WireGuard on some versions but not reliably on older hAP hardware. During the transition, V6 routers get an OpenVPN client instead:

- OpenVPN server is a companion service on the central host (`openvpn-server` systemd unit on `10.100.128.0/22`, separate from the WG `/22`).
- Same addressing approach: server allocates, config pushed to router during enrollment.

Since V6 is being retired opportunistically, I'd suggest building WireGuard first and only doing the OpenVPN work when a V6 router actually shows up that won't come up on WG. If your V6 routers are all on 6.45.x or later, try WG first — it often works.

### 4.5 Enrollment flow

There are **two enrollment paths** depending on how the router reached its install point:

| Path                | Used for                                                                    | Driver                                              |
|---------------------|-----------------------------------------------------------------------------|-----------------------------------------------------|
| **Zero-touch**      | New V7 stock that's been through Bradford's factory pre-provisioning script | Router phones home on first boot, no tech action    |
| **Manual**          | Retrofit of existing fleet, RMA returns, anything that missed factory prep  | Office/field tech pastes one-liner into router term |

Both paths converge on the same server state — a row in `routers`, a WG peer appended to `wg0.conf`, and a long-lived telemetry token. They differ only in how the enrollment is authorized (fresh one-shot token minted per router for manual, shared provisioning secret for zero-touch) and who kicks it off.

**Zero-touch** is covered in a companion doc, `02-self-provisioning.md`, including the factory script addition, the on-device self-enrollment script, the server endpoint (`POST /api/v1/auto-enroll`), and the schema additions for provisioning secret rotation. It's the primary path for new installs going forward.

The rest of §4.5 describes the **manual** flow. It's still first-class — needed for any router that didn't go through factory prep.

---

One-time, per-router. Tech does this either onsite or by pasting the bootstrap script into an already-reachable router:

```
  Router                                         Server
    │                                               │
    │ 1. Create WG interface (generates keypair)    │
    │ 2. Read own public key                        │
    │                                               │
    │ 3. POST /api/v1/enroll                        │
    │    { bootstrap_token, serial, mac,            │
    │      identity, model, ros_version,            │
    │      router_public_key }                      │
    ├──────────────────────────────────────────────►│
    │                                               │
    │                        4. Validate token      │
    │                        5. Allocate overlay IP │
    │                        6. Insert routers row  │
    │                        7. Append [Peer] to wg0│
    │                        8. `wg syncconf wg0`   │
    │                        9. Generate telemetry  │
    │                           push token          │
    │                                               │
    │◄──────────────────────────────────────────────┤
    │    { overlay_ip, server_public_key,           │
    │      server_endpoint, telemetry_token }       │
    │                                               │
    │ 10. Apply IP address to WG interface          │
    │ 11. Add peer w/ server pubkey + endpoint      │
    │ 12. Install telemetry scheduler + token       │
    │ 13. Done — first telemetry push in ≤5 min     │
```

Bootstrap tokens are single-use, short-lived (1 hour), minted on the server from an admin UI. The telemetry token is long-lived and unique per router; store it **hashed** in Postgres (like you'd store a password), not in plaintext.

### 4.6 Enrollment UX — office and field techs

Both office and field techs will enroll routers, so the flow needs to work equally well from a laptop browser in the office and from a phone browser sitting in a customer's driveway. The design goals:

- Minimum friction on the router side — a single paste into WinBox or SSH. No `.rsc` files to upload, no binary installers.
- The tech never handles raw secrets. The token is wrapped inside a one-liner that the tech copies; they don't need to understand what's in it.
- The tech identifies the customer by the naming scheme (`"hAP ac lite - Smith, John"`). That's the only field they have to think about.
- Works on any screen size, no JS framework required.

**The tech-facing flow, start to finish:**

1. Tech opens `https://mcc.bradfordbroadband.com/` on phone or laptop, logs in (session cookie from an admin login — future: SSO from your existing stack if there is one).
2. Taps **"Enroll a router"**. The form has three fields:
   - **Model** (dropdown: hAP lte / hAP ac lite / hAP ac² / hAP ac³ / hAP ax² / hAP ax³)
   - **Customer last name**
   - **Customer first name**
3. Server composes the identity (`"{model} - {LastName}, {FirstName}"`), checks it against UISP (Phase 2: if `uisp_client_id` is resolvable from the name, auto-link; Phase 1: just store the identity), mints a fresh enrollment token, and renders a **result page** containing:
   - The composed identity, for confirmation.
   - A **big copy-to-clipboard button** with the one-liner below.
   - A QR code of the one-liner (so the tech can scan into WinBox Mobile or copy into a terminal on a second device).
   - A live status indicator that turns green when the server sees the router complete enrollment. Auto-refreshes; if the tech walks away and comes back, they can see whether it finished.
4. Tech pastes the one-liner into the router's terminal (WinBox → New Terminal, or SSH):

   ```routeros
   /tool fetch url="https://mcc.bradfordbroadband.com/enroll/ABC123XYZ" mode=https dst-path=enroll.rsc; :delay 3s; /import enroll.rsc; /file remove enroll.rsc
   ```

   `ABC123XYZ` is the enrollment token — embedded in the URL, not passed as a header, so a plain `/tool fetch` works. The one-liner:
   - Downloads a server-rendered RouterOS script keyed to that token.
   - Imports it. The script is what actually does the work: creates the WG interface, reads its pubkey, POSTs enrollment data, applies the returned config, installs the telemetry script + scheduler.
   - Deletes the downloaded file when done. The enrollment token is single-use on the server side, so even if the file weren't cleaned up, it couldn't be replayed.

5. Server-rendered enrollment script (`GET /enroll/{token}`):
   - Validates the token hasn't been used or expired. If bad, returns a one-line RouterOS script that just logs an error and exits (so the tech sees `"enrollment token invalid"` in the router log instead of a confusing 404).
   - Looks up the partial `routers` row that was pre-created when the token was minted (identity, model).
   - Returns a RouterOS script populated with: server endpoint, server public key, server's eventual allocation-call URL, and the one-time callback token for reporting the router's pubkey.
   - Note: the enrollment callback still happens over HTTPS from the router to the server, so the server can record the router's pubkey and allocate the overlay IP. The enrollment script handles both halves — download config, then post back with router pubkey, then apply the returned overlay address.

**Why this shape and not a mobile app:**

- RouterOS doesn't have a clipboard import mechanism for anything but scripts. A one-liner that fetches + imports is the lowest-friction path on the device.
- A web UI + QR code covers the phone use-case without any app install. Field techs can use WinBox Mobile, or SSH via Termius, and paste from the clipboard.
- A full SPA is overkill for ~4 pages (login, router list, router detail, enroll). HTMX + a handful of Jinja templates will do it, fits in a few hundred lines of Python, and is trivial to modify later.

**Auth & authorization:**

- Office/field techs log in with username + password. In Phase 1, a single admin role is fine — everyone who can log in can enroll. Phase 2 can add roles if needed.
- Session cookie + CSRF token on state-changing routes.
- All enrollments are logged to the `audit_log` table with `actor = <tech username>`, `action = "enroll"`, and the resulting `router_id`.
- Rate-limit the enrollment endpoint: no more than N token mints per minute per user. Prevents accidental runaway scripts and small cost from leaked credentials.

**Failure / retry ergonomics:**

- If the tech pastes and nothing happens (router can't reach the server, WG port blocked upstream, etc.), the router's `/log print` will show the fetch error. The result page on the server side will stay "waiting" — the tech can refresh.
- If the token expired before the tech got to the router, the result page has a "Reissue token" button that generates a new one without re-entering the customer info.
- If enrollment partially completes (WG up but telemetry fails), the router still shows up in inventory with `last_seen_at` NULL — the admin can see it's stuck and investigate.

---

## 5. Telemetry push architecture

### 5.1 Transport

- Each router runs a scheduled script every 5 minutes.
- Script builds a JSON payload (details below) and POSTs it over HTTPS to `https://mcc.bradfordbroadband.com/api/v1/telemetry`.
- Authorization: `Authorization: Bearer <per-router-token>`.
- TLS verification is on. `/tool fetch` validates by default on modern RouterOS; don't disable.

Why HTTPS and not over the WG tunnel? Two reasons:

1. The tunnel being up shouldn't be a prerequisite for telemetry to flow. If WG is broken, we still want to know the router is alive and see its health data so we can diagnose.
2. Debuggable from outside. A techs-on-site can watch `journalctl -u nginx -f` and see a specific router's pushes coming in by source IP.

### 5.2 Payload schema (what each push contains)

```json
{
  "schema_version": 1,
  "timestamp": "2026-04-21T14:02:11Z",
  "identity": "cust-smith-john",
  "serial": "HC1234567A",
  "mac": "CC:2D:E0:12:34:56",
  "ros_version": "7.14.2",
  "board": "hAP ac2",
  "system": {
    "uptime_sec": 834521,
    "cpu_load_pct": 8,
    "free_memory_bytes": 56213504,
    "total_memory_bytes": 134217728,
    "temperature_c": 42,
    "voltage_v": 24.1
  },
  "ethernet": [
    { "name": "ether1", "running": true, "rate": "1Gbps",
      "rx_bytes": 123456789, "tx_bytes": 987654321,
      "rx_packets": 123456, "tx_packets": 98765 }
  ],
  "wireless_interfaces": [
    { "name": "wlan1", "ssid": "Smith-5G", "band": "5ghz-ac",
      "frequency": 5220, "channel_width": "20/40/80mhz-XXXX",
      "tx_power_dbm": 23, "disabled": false, "mode": "ap-bridge" }
  ],
  "clients": [
    { "interface": "wlan1", "mac": "A4:83:E7:AA:BB:CC",
      "signal_dbm": -62, "tx_rate_mbps": 650, "rx_rate_mbps": 780,
      "uptime_sec": 3601, "tx_bytes": 123456, "rx_bytes": 789012 }
  ],
  "wifi_stack": "wireless"
}
```

For ax2/ax3 models, `wireless_interfaces` becomes `wifi_interfaces` and `wifi_stack` is `"wifi"`. Same overall shape, same client structure. The server normalizes them into one Influx measurement.

### 5.3 Server ingest flow

1. nginx receives POST on `/api/v1/telemetry`, forwards to FastAPI.
2. FastAPI pulls `Authorization` header → looks up `router_tokens` → sets `router_id` on the request.
3. Pydantic schema validates the payload.
4. Updates to inventory (new SSIDs, version change, board info) go to Postgres via UPSERT.
5. Time-series points go to InfluxDB with tags `{router_id, model, wifi_stack}` and fields for all the numeric values.
6. Returns 204. On auth failure returns 401 and logs.

### 5.4 InfluxDB measurement layout

Three measurements:

- `system` — one point per push: cpu, memory, uptime, temperature, voltage.
- `interface` — one point per wireless/wifi/ethernet interface per push: tx/rx bytes, tx/rx packets, rate, signal stats (for wireless).
- `client` — one point per connected wireless client per push: signal, tx/rx rate, tx/rx bytes. Tagged with `{router_id, client_mac}`.

Retention:

- `system` + `interface`: 90 days raw, downsampled to hourly means kept 2 years.
- `client`: 30 days raw, downsampled to hourly kept 180 days. Client data is high cardinality; don't keep raw forever.

---

## 6. RouterOS telemetry scripts

Two separate scripts because the command trees are fundamentally different. The bootstrap script decides which to install based on whether `/interface wifi` exists.

### 6.1 Classic wireless stack — hAP lte / ac lite / ac² / ac³

Save on router as `telemetry-push` in `/system script`:

```routeros
# Telemetry push script — classic /interface wireless stack
# Scheduled every 5 minutes via /system scheduler

:local serverUrl  "https://mcc.bradfordbroadband.com/api/v1/telemetry"
:local authToken  "<INJECTED_AT_ENROLLMENT>"

# --- Build JSON payload ------------------------------------------------------
:local ts         [/system clock get date]
:local tsTime     [/system clock get time]
:local identity   [/system identity get name]
:local serial     ""
:do { :set serial [/system routerboard get serial-number] } on-error={}
:local board      ""
:do { :set board  [/system routerboard get model] } on-error={}
:local rosVer     [/system resource get version]
:local uptime     [/system resource get uptime]
:local cpuLoad    [/system resource get cpu-load]
:local freeMem    [/system resource get free-memory]
:local totalMem   [/system resource get total-memory]
:local mac        ""
:do { :set mac    [/interface ethernet get [find name=ether1] mac-address] } on-error={}
:local temp       0
:do { :set temp   [/system health get [find name=temperature] value] } on-error={}

:local payload    "{"
:set payload ($payload . "\"schema_version\":1,")
:set payload ($payload . "\"identity\":\"$identity\",")
:set payload ($payload . "\"serial\":\"$serial\",")
:set payload ($payload . "\"mac\":\"$mac\",")
:set payload ($payload . "\"board\":\"$board\",")
:set payload ($payload . "\"ros_version\":\"$rosVer\",")
:set payload ($payload . "\"wifi_stack\":\"wireless\",")
:set payload ($payload . "\"system\":{")
:set payload ($payload . "\"uptime\":\"$uptime\",")
:set payload ($payload . "\"cpu_load_pct\":$cpuLoad,")
:set payload ($payload . "\"free_memory_bytes\":$freeMem,")
:set payload ($payload . "\"total_memory_bytes\":$totalMem,")
:set payload ($payload . "\"temperature_c\":$temp")
:set payload ($payload . "},")

# Ethernet interfaces
:set payload ($payload . "\"ethernet\":[")
:local first true
:foreach e in=[/interface ethernet find] do={
    :if ($first = false) do={ :set payload ($payload . ",") }
    :set first false
    :local ename    [/interface ethernet get $e name]
    :local erunning [/interface ethernet get $e running]
    :local erxb     [/interface get $e rx-byte]
    :local etxb     [/interface get $e tx-byte]
    :local erxp     [/interface get $e rx-packet]
    :local etxp     [/interface get $e tx-packet]
    :set payload ($payload . "{\"name\":\"$ename\",\"running\":$erunning,")
    :set payload ($payload . "\"rx_bytes\":$erxb,\"tx_bytes\":$etxb,")
    :set payload ($payload . "\"rx_packets\":$erxp,\"tx_packets\":$etxp}")
}
:set payload ($payload . "],")

# Wireless interfaces
:set payload ($payload . "\"wireless_interfaces\":[")
:set first true
:foreach w in=[/interface wireless find] do={
    :if ($first = false) do={ :set payload ($payload . ",") }
    :set first false
    :local wname  [/interface wireless get $w name]
    :local wssid  [/interface wireless get $w ssid]
    :local wband  [/interface wireless get $w band]
    :local wfreq  [/interface wireless get $w frequency]
    :local wcw    [/interface wireless get $w channel-width]
    :local wtxp   [/interface wireless get $w tx-power]
    :local wdis   [/interface wireless get $w disabled]
    :local wmode  [/interface wireless get $w mode]
    :set payload ($payload . "{\"name\":\"$wname\",\"ssid\":\"$wssid\",")
    :set payload ($payload . "\"band\":\"$wband\",\"frequency\":\"$wfreq\",")
    :set payload ($payload . "\"channel_width\":\"$wcw\",\"tx_power\":\"$wtxp\",")
    :set payload ($payload . "\"disabled\":$wdis,\"mode\":\"$wmode\"}")
}
:set payload ($payload . "],")

# Wireless clients
:set payload ($payload . "\"clients\":[")
:set first true
:foreach c in=[/interface wireless registration-table find] do={
    :if ($first = false) do={ :set payload ($payload . ",") }
    :set first false
    :local cmac    [/interface wireless registration-table get $c mac-address]
    :local cint    [/interface wireless registration-table get $c interface]
    :local csig    [/interface wireless registration-table get $c signal-strength]
    :local ctx     [/interface wireless registration-table get $c tx-rate]
    :local crx     [/interface wireless registration-table get $c rx-rate]
    :local cup     [/interface wireless registration-table get $c uptime]
    :local cbtx    [/interface wireless registration-table get $c bytes]
    :set payload ($payload . "{\"mac\":\"$cmac\",\"interface\":\"$cint\",")
    :set payload ($payload . "\"signal\":\"$csig\",\"tx_rate\":\"$ctx\",")
    :set payload ($payload . "\"rx_rate\":\"$crx\",\"uptime\":\"$cup\",")
    :set payload ($payload . "\"bytes\":\"$cbtx\"}")
}
:set payload ($payload . "]}")

# --- POST to server ----------------------------------------------------------
:do {
    /tool fetch url=$serverUrl \
        http-method=post \
        http-header-field="Content-Type: application/json,Authorization: Bearer $authToken" \
        http-data=$payload \
        mode=https \
        keep-result=no \
        output=none
} on-error={
    :log warning "cpe-cloud telemetry push failed"
}
```

Scheduler entry:

```routeros
/system scheduler add name=cpe-cloud-telemetry \
    interval=5m start-time=startup \
    on-event="/system script run telemetry-push" \
    comment="CPE Cloud telemetry push"
```

A few notes on the script that are worth knowing:

- RouterOS scripting has no real JSON support, so we build strings by concatenation. It's ugly but reliable. Don't try to use `:serialize to=json` — support varies by minor version.
- `signal-strength` in the registration table is returned as a string like `"-62dBm@HTmcs15"` — we keep it as a string in the JSON and parse server-side. Trying to parse it in RouterOS is a pain.
- `uptime` is a duration string (`"2w3d4h"`). Same deal — stringify, parse on the server.
- The `on-error` on each `:do` block is defensive; some hAP models don't expose temperature or return different routerboard fields. We'd rather push a payload with some fields missing than have the script die silently.

### 6.2 WifiWave2 stack — hAP ax² / ax³

Same overall structure, different command tree:

```routeros
# Telemetry push script — /interface wifi (wave2) stack
# For hAP ax² and ax³ running RouterOS 7 with wifi-qcom / wifi-qcom-ac installed

:local serverUrl  "https://mcc.bradfordbroadband.com/api/v1/telemetry"
:local authToken  "<INJECTED_AT_ENROLLMENT>"

:local identity   [/system identity get name]
:local serial     ""
:do { :set serial [/system routerboard get serial-number] } on-error={}
:local board      ""
:do { :set board  [/system routerboard get model] } on-error={}
:local rosVer     [/system resource get version]
:local uptime     [/system resource get uptime]
:local cpuLoad    [/system resource get cpu-load]
:local freeMem    [/system resource get free-memory]
:local totalMem   [/system resource get total-memory]
:local mac        ""
:do { :set mac    [/interface ethernet get [find name=ether1] mac-address] } on-error={}
:local temp       0
:do { :set temp   [/system health get [find name=temperature] value] } on-error={}

:local payload    "{"
:set payload ($payload . "\"schema_version\":1,")
:set payload ($payload . "\"identity\":\"$identity\",")
:set payload ($payload . "\"serial\":\"$serial\",")
:set payload ($payload . "\"mac\":\"$mac\",")
:set payload ($payload . "\"board\":\"$board\",")
:set payload ($payload . "\"ros_version\":\"$rosVer\",")
:set payload ($payload . "\"wifi_stack\":\"wifi\",")
:set payload ($payload . "\"system\":{")
:set payload ($payload . "\"uptime\":\"$uptime\",")
:set payload ($payload . "\"cpu_load_pct\":$cpuLoad,")
:set payload ($payload . "\"free_memory_bytes\":$freeMem,")
:set payload ($payload . "\"total_memory_bytes\":$totalMem,")
:set payload ($payload . "\"temperature_c\":$temp")
:set payload ($payload . "},")

# Ethernet
:set payload ($payload . "\"ethernet\":[")
:local first true
:foreach e in=[/interface ethernet find] do={
    :if ($first = false) do={ :set payload ($payload . ",") }
    :set first false
    :local ename    [/interface ethernet get $e name]
    :local erunning [/interface ethernet get $e running]
    :local erxb     [/interface get $e rx-byte]
    :local etxb     [/interface get $e tx-byte]
    :set payload ($payload . "{\"name\":\"$ename\",\"running\":$erunning,")
    :set payload ($payload . "\"rx_bytes\":$erxb,\"tx_bytes\":$etxb}")
}
:set payload ($payload . "],")

# WiFi interfaces (wave2)
:set payload ($payload . "\"wifi_interfaces\":[")
:set first true
:foreach w in=[/interface wifi find] do={
    :if ($first = false) do={ :set payload ($payload . ",") }
    :set first false
    :local wname     [/interface wifi get $w name]
    :local wdis      [/interface wifi get $w disabled]
    :local wconfig   ""
    :do { :set wconfig [/interface wifi get $w configuration] } on-error={}
    :local wssid     ""
    :local wchan     ""
    :do { :set wssid [/interface wifi get $w ssid] } on-error={}
    :do { :set wchan [/interface wifi get $w channel] } on-error={}
    :set payload ($payload . "{\"name\":\"$wname\",\"ssid\":\"$wssid\",")
    :set payload ($payload . "\"channel\":\"$wchan\",\"disabled\":$wdis,")
    :set payload ($payload . "\"configuration\":\"$wconfig\"}")
}
:set payload ($payload . "],")

# WiFi clients
:set payload ($payload . "\"clients\":[")
:set first true
:foreach c in=[/interface wifi registration-table find] do={
    :if ($first = false) do={ :set payload ($payload . ",") }
    :set first false
    :local cmac    [/interface wifi registration-table get $c mac-address]
    :local cint    [/interface wifi registration-table get $c interface]
    :local csig    [/interface wifi registration-table get $c rx-signal]
    :local ctx     [/interface wifi registration-table get $c tx-rate]
    :local crx     [/interface wifi registration-table get $c rx-rate]
    :local cup     [/interface wifi registration-table get $c uptime]
    :set payload ($payload . "{\"mac\":\"$cmac\",\"interface\":\"$cint\",")
    :set payload ($payload . "\"signal\":\"$csig\",\"tx_rate\":\"$ctx\",")
    :set payload ($payload . "\"rx_rate\":\"$crx\",\"uptime\":\"$cup\"}")
}
:set payload ($payload . "]}")

:do {
    /tool fetch url=$serverUrl \
        http-method=post \
        http-header-field="Content-Type: application/json,Authorization: Bearer $authToken" \
        http-data=$payload \
        mode=https \
        keep-result=no \
        output=none
} on-error={
    :log warning "cpe-cloud telemetry push failed"
}
```

WifiWave2 has moved around quite a bit between RouterOS 7.x versions. Fields I'm using (`rx-signal`, `tx-rate`, `rx-rate` on `registration-table`) are stable from 7.13 onward. If you have ax-series on 7.10–7.12, test before rolling out — `rx-signal` used to be just `signal`.

### 6.3 Stack detection at enrollment

The bootstrap script decides which of the two scripts above to install:

```routeros
:local hasWifi false
:do { [/interface wifi find]; :set hasWifi true } on-error={}

:if ($hasWifi) do={
    # install wifi-telemetry.rsc
} else={
    # install wireless-telemetry.rsc
}
```

The `[/interface wifi find]` call errors out on hardware/firmware that doesn't have the command tree at all, which is how we detect classic-only boards.

---

## 7. Postgres schema

```sql
-- Primary inventory
CREATE TABLE routers (
    id                  BIGSERIAL PRIMARY KEY,

    -- Identity is the human-facing primary label, following Bradford's naming
    -- scheme: "{model} - {LastName}, {FirstName}" (e.g. "hAP ac lite - Smith, John").
    -- Enforced as unique + not-null. The convention is validated in the app layer,
    -- not via CHECK constraint, so we can relax it later without a migration.
    identity            TEXT UNIQUE NOT NULL,

    serial_number       TEXT UNIQUE NOT NULL,
    mac_address         MACADDR UNIQUE NOT NULL,
    model               TEXT,
    ros_version         TEXT,
    ros_major           SMALLINT,                  -- 6 or 7
    wifi_stack          TEXT CHECK (wifi_stack IN ('wireless', 'wifi')),

    -- UISP join keys (populated later, Phase 2)
    uisp_client_id      TEXT,
    uisp_service_id     TEXT,
    uisp_site_id        TEXT,
    linked_airmax_id    TEXT,

    -- Overlay
    wg_public_key       TEXT UNIQUE,
    wg_overlay_ip       INET UNIQUE,

    -- Lifecycle
    enrolled_at         TIMESTAMPTZ,
    last_seen_at        TIMESTAMPTZ,
    status              TEXT DEFAULT 'active',     -- active | decommissioned | quarantined
    notes               TEXT,
    created_at          TIMESTAMPTZ DEFAULT NOW(),
    updated_at          TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_routers_uisp_client ON routers(uisp_client_id)
    WHERE uisp_client_id IS NOT NULL;
CREATE INDEX idx_routers_last_seen   ON routers(last_seen_at);
CREATE INDEX idx_routers_status      ON routers(status);
-- Partial pattern index for fast "search by last name" lookups in the admin UI.
CREATE INDEX idx_routers_identity_trgm ON routers USING gin (identity gin_trgm_ops);
-- Requires: CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- Auth tokens for telemetry pushes (one per router, hashed)
CREATE TABLE router_tokens (
    id                  BIGSERIAL PRIMARY KEY,
    router_id           BIGINT NOT NULL REFERENCES routers(id) ON DELETE CASCADE,
    token_hash          TEXT NOT NULL,             -- argon2 or sha256
    token_prefix        TEXT NOT NULL,             -- first 8 chars, for lookup hint
    created_at          TIMESTAMPTZ DEFAULT NOW(),
    revoked_at          TIMESTAMPTZ
);
CREATE INDEX idx_router_tokens_prefix ON router_tokens(token_prefix) WHERE revoked_at IS NULL;

-- One-shot enrollment tokens (short-lived, single-use)
CREATE TABLE enrollment_tokens (
    id                  BIGSERIAL PRIMARY KEY,
    token_hash          TEXT UNIQUE NOT NULL,
    issued_by           TEXT,                      -- admin identifier
    expires_at          TIMESTAMPTZ NOT NULL,
    used_at             TIMESTAMPTZ,
    used_by_router_id   BIGINT REFERENCES routers(id),
    created_at          TIMESTAMPTZ DEFAULT NOW()
);

-- Current interface state (last-write-wins snapshot)
CREATE TABLE router_interfaces (
    id                  BIGSERIAL PRIMARY KEY,
    router_id           BIGINT NOT NULL REFERENCES routers(id) ON DELETE CASCADE,
    name                TEXT NOT NULL,
    kind                TEXT NOT NULL,             -- ethernet | wireless | wifi
    ssid                TEXT,
    band                TEXT,
    frequency_mhz       INTEGER,
    channel_width       TEXT,
    tx_power_dbm        INTEGER,
    mode                TEXT,
    disabled            BOOLEAN,
    last_updated        TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (router_id, name)
);

-- Audit log for future control-plane actions (Phase 2+)
-- Included here so schema is stable from day one.
CREATE TABLE audit_log (
    id                  BIGSERIAL PRIMARY KEY,
    router_id           BIGINT REFERENCES routers(id),
    actor               TEXT NOT NULL,
    action              TEXT NOT NULL,
    params              JSONB,
    status              TEXT NOT NULL,             -- pending | success | failed
    result              JSONB,
    created_at          TIMESTAMPTZ DEFAULT NOW(),
    completed_at        TIMESTAMPTZ
);
CREATE INDEX idx_audit_router      ON audit_log(router_id);
CREATE INDEX idx_audit_created_at  ON audit_log(created_at DESC);

-- Phase 2 placeholder — defining now so migrations stay clean
CREATE TABLE router_backups (
    id                  BIGSERIAL PRIMARY KEY,
    router_id           BIGINT NOT NULL REFERENCES routers(id) ON DELETE CASCADE,
    kind                TEXT NOT NULL,             -- binary | export
    storage_path        TEXT NOT NULL,
    size_bytes          BIGINT,
    sha256              TEXT,
    taken_at            TIMESTAMPTZ DEFAULT NOW()
);
```

Rationale for a few choices:

- **`routers.identity`** is the human-facing primary label. Bradford's naming convention `"{model} - {LastName}, {FirstName}"` (e.g. `"hAP ac lite - Smith, John"`) is kept as the single source of truth for tech-facing references. Unique + not-null. Trigram index lets the admin search-by-name field do fuzzy matching as you type.
- `routers.serial_number` stays unique but is secondary — stamped on the case, useful for hardware audits and RMAs, not something techs will type.
- Surrogate `id` is for FK joins and stable URLs in the admin UI (avoids URL-encoding commas + spaces from the identity).
- `wg_overlay_ip` is `INET`, unique. IP allocation happens in a transaction: `INSERT INTO routers ... RETURNING id, wg_overlay_ip` with the IP picked by a function that finds the lowest unused host in `10.100.0.0/22`.
- `router_tokens.token_prefix` lets us look up a token without scanning the whole table. Standard pattern: take the first 8 chars of the raw token, store hash of the rest.
- `router_interfaces` is snapshot state, not history. History lives in InfluxDB.
- `audit_log` is here now even though we don't populate it in Phase 1. Adding a log table later and then backfilling references is always worse than just having it.

---

## 8. Python server skeleton

Dependency baseline (`requirements.txt`):

```
fastapi==0.115.*
uvicorn[standard]==0.30.*
sqlalchemy==2.0.*
alembic==1.13.*
psycopg[binary]==3.2.*
pydantic==2.*
pydantic-settings==2.*
influxdb-client==1.46.*
argon2-cffi==23.*
python-multipart==0.0.*
httpx==0.27.*
```

Key modules to build first:

**`app/services/wireguard.py`** — regenerates `/etc/wireguard/wg0.conf` from the Postgres `routers` table and runs `wg syncconf wg0 <(wg-quick strip wg0)` to apply changes without dropping existing tunnels. Called on enrollment and on router removal. This is the one component that needs root (for `wg` and to write `/etc/wireguard/`). Run the FastAPI service as an unprivileged user and have `wireguard.py` shell out via a small sudo-wrapped helper script — don't run the web app as root.

**`app/services/influx.py`** — thin wrapper over `influxdb-client` that takes a normalized telemetry payload and writes the three measurements (`system`, `interface`, `client`) with appropriate tags.

**`app/routers/enrollment.py`** — single endpoint `POST /api/v1/enroll` that validates a one-shot enrollment token, allocates an overlay IP, mints a long-lived telemetry token, writes the `routers` + `router_tokens` rows, appends to `wg0.conf`, and returns the config the router needs.

**`app/routers/telemetry.py`** — `POST /api/v1/telemetry` that validates the bearer token, Pydantic-parses the payload, upserts `router_interfaces`, bumps `last_seen_at`, and writes Influx points.

I can scaffold all of these when you're ready — say the word and I'll generate the full `/opt/cpe-cloud/app/` tree as the next step.

---

## 9. Ubuntu 22.04 bootstrap — high-level order of operations

A rough runbook for standing up the central server. Not a script yet; we'll turn this into Ansible or a shell install script later.

1. Fresh Ubuntu 22.04 droplet. SSH hardened (key-only, UFW allow 22/443/51820).
2. `apt install -y wireguard wireguard-tools nginx postgresql-15 python3.11-venv`
3. Install InfluxDB 2.x from the official apt repo; create an org, a bucket (`cpe-cloud`), and an admin token. Save token to `/opt/cpe-cloud/.env`.
4. Create DB + user in Postgres. Run the Alembic migration for the schema in §7.
5. Generate server WireGuard keypair. Write `/etc/wireguard/wg0.conf` with just the `[Interface]` stanza (no peers yet). `systemctl enable --now wg-quick@wg0`.
6. Clone the app into `/opt/cpe-cloud/`, set up venv, install deps.
7. Create `cpe-cloud.service` systemd unit running uvicorn on `127.0.0.1:8000`.
8. Configure nginx: TLS via certbot, reverse-proxy `/api/v1/*` → `127.0.0.1:8000`. Enforce HTTPS (HSTS), set reasonable body-size limits (`client_max_body_size 256k;` is plenty for a telemetry payload — anything bigger is suspicious).
9. Mint the first enrollment token through a CLI helper (`python -m app.cli issue-enrollment-token --ttl 1h`).
10. On a test Mikrotik, run the bootstrap script with that token. Watch `journalctl -u cpe-cloud -f` and `wg show` to confirm the tunnel and first push.

---

## 10. Risks & things to watch

- **RouterOS `/tool fetch` JSON payload size.** Quietly caps around 4 KB of `http-data` on some firmware versions. A hAP with 30+ wireless clients and verbose fields can bump up against this. Mitigation: if a payload is going to exceed ~3 KB, split the push into two calls (system+interfaces, then clients). We'll add this check once we see it in the wild.
- **Wildly different clock skew across the fleet.** hAP routers without NTP configured will send timestamps that are off by hours. Solution: trust server timestamps for storage, but keep router timestamps in the payload for diagnostic purposes. Also: configure NTP in the bootstrap script.
- **WireGuard NAT traversal from double-NAT.** Some customers are behind CGNAT on their upstream *and* the AirMax NAT. WG should handle this since it's UDP + keepalive, but if a router can't establish a tunnel, the telemetry push still works — we'll notice the router in inventory but not in `wg show`, and that's a signal to investigate.
- **WifiWave2 field churn.** I flagged this above. Track it in a `docs/routeros-version-matrix.md` file as we verify specific version-field combinations in production.
- **Enrollment token leakage.** One-time, short-lived, hashed at rest. But if an attacker gets one before the tech uses it, they could enroll a rogue device. Mitigation: admin UI should show live token status, and the allocated overlay IP is in an isolated subnet with no routes anywhere interesting until we explicitly add them. Even an enrolled rogue device can only talk to the server.

---

## 11. Decisions locked in

| # | Question                           | Decision                                                                                                          | Date       |
|---|------------------------------------|-------------------------------------------------------------------------------------------------------------------|------------|
| 1 | Central server hostname            | **`mcc.bradfordbroadband.com`** (needs A record + Let's Encrypt cert)                                             | 2026-04-21 |
| 2 | Where to host the new VM           | **Same DO account as UISP, separate droplet.** Simplifies billing; keeps blast radius isolated from UISP.         | 2026-04-21 |
| 3 | Who does enrollment                | **Both office and field techs.** Two enrollment paths: zero-touch (primary, for new stock) + manual (retrofit).   | 2026-04-21 |
| 4 | Primary human-facing router ID     | **`identity` field**, following `"{model} - {LastName}, {FirstName}"` (e.g. `"hAP ac lite - Smith, John"`).       | 2026-04-21 |
| 5 | How do new routers enroll          | **Zero-touch self-provisioning** via factory prep script + shared provisioning secret. See `02-self-provisioning.md`. | 2026-04-21 |
| 6 | Auto-approve rule for auto-enrolls | **Identity-pattern match.** If `identity` matches `^hAP .+ - .+, .+$`, status → `active`. Otherwise → `pending`.  | 2026-04-21 |
| 7 | How is the enrollment script delivered to new routers | **Hosted installer at `/factory/self-enroll.rsc`** — factory prep downloads & `/import`s it, avoiding nested-quote escaping. Admin fetch token authorizes download; provisioning secret is embedded server-side. Fallback fully-inlined version in Appendix A of `02-self-provisioning.md`. | 2026-04-21 |

---

## 12. Next deliverables

With the above locked in, the logical next pieces of work are:

1. **Python app scaffold** — the FastAPI skeleton under `/opt/cpe-cloud/app/`, with empty-but-wired enrollment and telemetry endpoints, SQLAlchemy models matching §7, and a `.env` template.
2. **Alembic initial migration** — the schema in §7 (plus the `provisioning_secrets` and `provisioning_rules` additions from `02-self-provisioning.md` §4.5) as a real migration, enabling `pg_trgm`, indexes and all.
3. **WireGuard sync service** — `app/services/wireguard.py` that regenerates `wg0.conf` from Postgres and shells out to `wg syncconf`. The piece that needs a sudo helper.
4. **Factory installer endpoint** — `GET /factory/self-enroll.rsc` per `02-self-provisioning.md` §3.2, plus `factory-installer.rsc.j2` template (§3.3), plus the admin fetch token CLI/web UI.
5. **Auto-enrollment endpoint** — `POST /api/v1/auto-enroll` per `02-self-provisioning.md` §4.4, plus the provision.rsc.j2 template.
6. **Manual enrollment endpoint + field-tech web UI** — `POST /api/v1/enroll` plus the mobile-friendly page (HTMX + Jinja; no SPA needed) implementing the flow in §4.6.
7. **CLI for ops** — `python -m app.cli` subcommands for minting/rotating provisioning secrets, fetch tokens, approving pending routers, resetting enrollments (per `02-self-provisioning.md` §7).
8. **Lab validation** — test both enrollment paths against a lab hAP (fresh factory-prepped unit + an already-provisioned retrofit unit) before rolling out to any live customer.

The natural build order is 1 → 2 → 3 → 4 → 5 → 7 → 6 → 8 (ops CLI before UI so we can validate without a UI first). If you want to skip straight to validating zero-touch on a single lab router without the UI, the minimum viable path is 1 → 2 → 3 → 4 → 5 → 7 → 8.
