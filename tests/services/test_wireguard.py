"""Unit tests for app.services.wireguard pure helpers.

These tests don't need a Postgres instance — they exercise the config
rendering + file helpers in isolation. Run with ``pytest -q tests/``.

Integration tests for ``sync_from_db`` (which touches the DB and calls the
sudo helper) belong in a later tests/integration/ tree that runs against a
real droplet.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.services.wireguard import (
    MANAGED_MARKER,
    PeerRow,
    _read_interface_block,
    _render_config,
    _write_atomic,
)


# ---------------------------------------------------------------------------
# _read_interface_block
# ---------------------------------------------------------------------------

def test_read_interface_block_no_peers(tmp_path: Path) -> None:
    """A conf with only [Interface] should come back unchanged (trimmed)."""
    conf = tmp_path / "wg0.conf"
    conf.write_text(
        "[Interface]\n"
        "Address = 10.100.0.1/22\n"
        "ListenPort = 51820\n"
        "PrivateKey = SERVER_PRIVATE_KEY_PLACEHOLDER\n"
    )
    got = _read_interface_block(conf)
    assert "[Interface]" in got
    assert "ListenPort = 51820" in got
    assert "PrivateKey = SERVER_PRIVATE_KEY_PLACEHOLDER" in got
    assert "[Peer]" not in got


def test_read_interface_block_strips_existing_peers(tmp_path: Path) -> None:
    """If peers exist, _read_interface_block must drop everything from the
    first [Peer] onward. This is how we replace stale peers without ever
    touching the server's private key."""
    conf = tmp_path / "wg0.conf"
    conf.write_text(
        "[Interface]\n"
        "Address = 10.100.0.1/22\n"
        "PrivateKey = SECRET\n"
        "\n"
        "# Smith, John\n"
        "[Peer]\n"
        "PublicKey = OLD_PEER_PUBKEY\n"
        "AllowedIPs = 10.100.0.2/32\n"
    )
    got = _read_interface_block(conf)
    assert "PrivateKey = SECRET" in got
    assert "[Peer]" not in got
    assert "OLD_PEER_PUBKEY" not in got


def test_read_interface_block_strips_at_managed_marker(tmp_path: Path) -> None:
    """The managed-marker comment is a secondary split point (cheap belt-and-
    suspenders if a future refactor changes marker styles)."""
    conf = tmp_path / "wg0.conf"
    conf.write_text(
        "[Interface]\n"
        "PrivateKey = KEEP\n"
        "\n"
        f"{MANAGED_MARKER}\n"
        "\n"
        "[Peer]\n"
        "PublicKey = PK\n"
        "AllowedIPs = 10.100.0.5/32\n"
    )
    got = _read_interface_block(conf)
    assert "PrivateKey = KEEP" in got
    assert MANAGED_MARKER not in got
    assert "[Peer]" not in got


def test_read_interface_block_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="INSTALL.md"):
        _read_interface_block(tmp_path / "does-not-exist.conf")


# ---------------------------------------------------------------------------
# _render_config
# ---------------------------------------------------------------------------

def test_render_config_no_peers() -> None:
    out = _render_config("[Interface]\nPrivateKey = SECRET\n", [])
    assert "[Interface]" in out
    assert "PrivateKey = SECRET" in out
    assert MANAGED_MARKER in out
    assert "[Peer]" not in out


def test_render_config_orders_and_labels_peers() -> None:
    peers = [
        PeerRow(
            identity="hAP ac lite - Smith, John",
            public_key="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa0=",
            overlay_ip="10.100.0.2",
        ),
        PeerRow(
            identity="hAP ax2 - Doe, Jane",
            public_key="bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb0=",
            overlay_ip="10.100.0.3",
        ),
    ]
    out = _render_config("[Interface]\nPrivateKey = S\n", peers)

    # Both peers present, in order.
    smith_pos = out.find("Smith, John")
    doe_pos = out.find("Doe, Jane")
    assert 0 < smith_pos < doe_pos

    # Each peer has the expected AllowedIPs + PersistentKeepalive.
    assert "AllowedIPs = 10.100.0.2/32" in out
    assert "AllowedIPs = 10.100.0.3/32" in out
    assert out.count("PersistentKeepalive = 25") == 2

    # Identities are used as comments above each [Peer] for operator readability.
    assert "# hAP ac lite - Smith, John" in out


def test_render_config_ends_with_newline() -> None:
    """wg-quick is pedantic — configs should end with exactly one newline."""
    out = _render_config("[Interface]\n", [])
    assert out.endswith("\n")
    assert not out.endswith("\n\n")


# ---------------------------------------------------------------------------
# _write_atomic
# ---------------------------------------------------------------------------

def test_write_atomic_replaces_content(tmp_path: Path) -> None:
    target = tmp_path / "wg0.conf"
    target.write_text("old content\n")
    target.chmod(0o640)

    _write_atomic(target, "new content\n")

    assert target.read_text() == "new content\n"
    # Mode preserved.
    assert (target.stat().st_mode & 0o777) == 0o640


def test_write_atomic_cleans_up_on_error(tmp_path: Path, monkeypatch) -> None:
    """If the write raises mid-way, no .tmp files should be left behind."""
    target = tmp_path / "wg0.conf"
    target.write_text("old\n")

    # Force os.replace to fail — simulates a filesystem error.
    def boom(*args, **kwargs):
        raise OSError("simulated disk failure")

    monkeypatch.setattr("os.replace", boom)
    with pytest.raises(OSError, match="simulated"):
        _write_atomic(target, "new\n")

    # No lingering temp files.
    leftovers = [p for p in tmp_path.iterdir() if p.name.startswith(".wg0.")]
    assert leftovers == [], f"temp file(s) left behind: {leftovers}"
    # Target is unchanged.
    assert target.read_text() == "old\n"
