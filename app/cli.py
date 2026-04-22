"""Operations CLI.

Run with: ``python -m app.cli <command>``

Live commands:
  * ``wg sync`` / ``wg ip-alloc-peek`` - WireGuard overlay operations
  * ``fetch-tokens mint/revoke/list`` - admin fetch tokens for factory prep

Still stubbed (deliverable #7): ``provisioning-secret``, ``enrollment-token``,
``approve``.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys


def _cmd_stub(args: argparse.Namespace) -> int:
    print(
        f"[stub] {args.cmd} {getattr(args, 'subcmd', '')} not yet implemented",
        file=sys.stderr,
    )
    return 2


def _current_admin_username() -> str:
    """Best-effort username for `issued_by`. Falls back to $USER / $LOGNAME."""
    return (
        os.environ.get("SUDO_USER")
        or os.environ.get("USER")
        or os.environ.get("LOGNAME")
        or "unknown"
    )


# ---------------------------------------------------------------------------
# wg
# ---------------------------------------------------------------------------

async def _wg_sync_async() -> int:
    from app.db import sessionmaker
    from app.services import wireguard

    async with sessionmaker()() as session:
        result = await wireguard.sync_from_db(session)
    print(f"wireguard sync complete: {result['peers_synced']} peer(s)")
    print(f"config path: {result['config_path']}")
    return 0


def _cmd_wg_sync(args: argparse.Namespace) -> int:
    return asyncio.run(_wg_sync_async())


async def _wg_ip_alloc_peek_async() -> int:
    from app.db import sessionmaker
    from app.services import wireguard

    async with sessionmaker()() as session:
        ip = await wireguard.allocate_overlay_ip(session)
    print(f"next free overlay IP would be: {ip}")
    print("(this is a dry run - nothing inserted into routers table)")
    return 0


def _cmd_wg_ip_alloc_peek(args: argparse.Namespace) -> int:
    return asyncio.run(_wg_ip_alloc_peek_async())


# ---------------------------------------------------------------------------
# fetch-tokens
# ---------------------------------------------------------------------------

async def _ft_mint_async(label: str, ttl_hours: int) -> int:
    from app.db import sessionmaker
    from app.services.tokens import issue_admin_fetch_token

    async with sessionmaker()() as session:
        result = await issue_admin_fetch_token(
            session,
            label=label,
            ttl_hours=ttl_hours,
            issued_by=_current_admin_username(),
        )
        await session.commit()

    print("=" * 70)
    print("Admin fetch token minted. Copy this now -- it will not be shown again.")
    print("=" * 70)
    print(f"  Label    : {result.row.label}")
    print(f"  Token    : {result.raw}")
    print(f"  Prefix   : {result.row.token_prefix}")
    print(f"  Expires  : {result.row.expires_at.isoformat(timespec='seconds')}")
    print(f"  Issued by: {result.row.issued_by or 'unknown'}")
    print("=" * 70)
    print(
        "Use in factory-prep script:\n"
        f'  /tool fetch url="https://mcc.bradfordbroadband.com/factory/self-enroll.rsc?t={result.raw}" \\\n'
        '      dst-path=cpe-cloud-installer.rsc mode=https'
    )
    return 0


def _cmd_ft_mint(args: argparse.Namespace) -> int:
    return asyncio.run(_ft_mint_async(args.label, args.ttl_hours))


async def _ft_revoke_async(ident: str) -> int:
    from app.db import sessionmaker
    from app.services.tokens import revoke_admin_fetch_token

    async with sessionmaker()() as session:
        count = await revoke_admin_fetch_token(session, ident)
        await session.commit()

    if count == 0:
        print(f"no active tokens matched {ident!r}", file=sys.stderr)
        return 1
    print(f"revoked {count} token(s) matching {ident!r}")
    return 0


def _cmd_ft_revoke(args: argparse.Namespace) -> int:
    return asyncio.run(_ft_revoke_async(args.ident))


async def _ft_list_async(include_inactive: bool) -> int:
    from app.db import sessionmaker
    from app.services.tokens import list_admin_fetch_tokens

    async with sessionmaker()() as session:
        rows = await list_admin_fetch_tokens(
            session, include_inactive=include_inactive
        )

    if not rows:
        print("(no tokens)" + (" matching filters" if not include_inactive else ""))
        return 0

    hdr = (
        f"{'ID':>4}  {'PREFIX':<10}  {'LABEL':<28}  {'ISSUED_BY':<14}  "
        f"{'EXPIRES':<20}  {'USES':>4}  STATUS"
    )
    print(hdr)
    print("-" * len(hdr))
    for r in rows:
        status = "active" if r.active else (
            "revoked" if r.revoked_at is not None else "expired"
        )
        print(
            f"{r.id:>4}  {r.prefix:<10}  {r.label[:28]:<28}  "
            f"{(r.issued_by or '-')[:14]:<14}  "
            f"{r.expires_at.strftime('%Y-%m-%dT%H:%M:%S'):<20}  "
            f"{r.use_count:>4}  {status}"
        )
    return 0


def _cmd_ft_list(args: argparse.Namespace) -> int:
    return asyncio.run(_ft_list_async(args.all))


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="app.cli", description="CPE Cloud ops CLI")
    sub = p.add_subparsers(dest="cmd", required=True)

    # wg ---------------------------------------------------------------------
    wg = sub.add_parser("wg", help="WireGuard overlay operations")
    wg_sub = wg.add_subparsers(dest="subcmd", required=True)

    wg_sync = wg_sub.add_parser(
        "sync",
        help="Regenerate wg0.conf from Postgres and reload WireGuard",
    )
    wg_sync.set_defaults(func=_cmd_wg_sync)

    wg_peek = wg_sub.add_parser(
        "ip-alloc-peek",
        help="Show the IP that would be allocated to the next enrollment",
    )
    wg_peek.set_defaults(func=_cmd_wg_ip_alloc_peek)

    # fetch-tokens -----------------------------------------------------------
    ft = sub.add_parser("fetch-tokens", help="Manage admin fetch tokens")
    ft_sub = ft.add_subparsers(dest="subcmd", required=True)

    ft_mint = ft_sub.add_parser(
        "mint",
        help="Mint a new admin fetch token (prints raw token once)",
    )
    ft_mint.add_argument(
        "--label",
        required=True,
        help='Human-readable label, e.g. "bench-2" or "Aaron prep 2026-04-21"',
    )
    ft_mint.add_argument(
        "--ttl-hours",
        type=int,
        default=168,
        help="Lifetime in hours (default 168 = 7 days)",
    )
    ft_mint.set_defaults(func=_cmd_ft_mint)

    ft_revoke = ft_sub.add_parser(
        "revoke",
        help="Revoke one or more active tokens by label or token_prefix",
    )
    ft_revoke.add_argument("ident", help="Label or first 8 chars of the raw token")
    ft_revoke.set_defaults(func=_cmd_ft_revoke)

    ft_list = ft_sub.add_parser("list", help="List admin fetch tokens")
    ft_list.add_argument(
        "--all",
        action="store_true",
        help="Include revoked/expired tokens (default: active only)",
    )
    ft_list.set_defaults(func=_cmd_ft_list)

    # provisioning-secret ----------------------------------------------------
    ps = sub.add_parser("provisioning-secret", help="Manage provisioning secrets")
    ps_sub = ps.add_subparsers(dest="subcmd", required=True)

    ps_rot = ps_sub.add_parser("rotate", help="Rotate to a new current secret")
    ps_rot.add_argument("--grace-days", type=int, default=60)
    ps_rot.set_defaults(func=_cmd_stub)

    ps_list = ps_sub.add_parser(
        "list", help="List active/previous secrets (hashes only)"
    )
    ps_list.set_defaults(func=_cmd_stub)

    # enrollment-token (manual flow) -----------------------------------------
    et = sub.add_parser("enrollment-token", help="One-shot manual enrollment tokens")
    et_sub = et.add_subparsers(dest="subcmd", required=True)

    et_issue = et_sub.add_parser("issue", help="Issue a new enrollment token")
    et_issue.add_argument("--ttl", default="1h")
    et_issue.set_defaults(func=_cmd_stub)

    # approve ----------------------------------------------------------------
    ap = sub.add_parser("approve", help="Approve a pending router")
    ap.add_argument("identity_or_id")
    ap.set_defaults(func=_cmd_stub)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
