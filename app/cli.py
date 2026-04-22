"""Operations CLI.

Run with: ``python -m app.cli <command>``

Most subcommands are still scaffolds (deliverable #7 fills them in). The
``wg`` subcommands are live - they exercise the real
``app.services.wireguard`` code path so you can test it without enrolling a
router.
"""
from __future__ import annotations

import argparse
import asyncio
import sys


def _cmd_stub(args: argparse.Namespace) -> int:
    print(
        f"[stub] {args.cmd} {getattr(args, 'subcmd', '')} not yet implemented",
        file=sys.stderr,
    )
    return 2


# ---------------------------------------------------------------------------
# Live commands
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
    # Peek only - we don't insert anything, so the IP isn't actually reserved.
    print(f"next free overlay IP would be: {ip}")
    print("(this is a dry run - nothing inserted into routers table)")
    return 0


def _cmd_wg_ip_alloc_peek(args: argparse.Namespace) -> int:
    return asyncio.run(_wg_ip_alloc_peek_async())


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

    ft_mint = ft_sub.add_parser("mint", help="Mint a new admin fetch token")
    ft_mint.add_argument("--label", required=True)
    ft_mint.add_argument("--ttl-hours", type=int, default=24)
    ft_mint.set_defaults(func=_cmd_stub)

    ft_revoke = ft_sub.add_parser("revoke", help="Revoke by label or prefix")
    ft_revoke.add_argument("ident")
    ft_revoke.set_defaults(func=_cmd_stub)

    ft_list = ft_sub.add_parser("list", help="List active fetch tokens")
    ft_list.set_defaults(func=_cmd_stub)

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
