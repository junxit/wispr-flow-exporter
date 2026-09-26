"""The suite's own guard rails, asserted so they cannot quietly disappear.

Every test runs inside the autouse fixture in ``conftest.py``: no network, no
inherited ``WISPR_*`` settings, an empty credential store and a scratch working
directory. Those guards exist because tests have reached real data before, so
they are checked here rather than trusted.
"""

from __future__ import annotations

import os
import socket
from pathlib import Path

import pytest


def test_a_test_cannot_reach_the_network() -> None:
    """192.0.2.1 is TEST-NET-1: never routable, and refused before a packet."""
    with pytest.raises(RuntimeError, match="reach the network"):
        socket.create_connection(("192.0.2.1", 443), timeout=0.01)


def test_loopback_stays_reachable_for_the_login_listener() -> None:
    """The OAuth callback tests need 127.0.0.1, so the guard must allow it."""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            pass
    finally:
        listener.close()


def test_a_test_never_sees_the_developers_settings(tmp_path: Path) -> None:
    """Only the three settings the fixture chose, all pointing into tmp_path."""
    wispr = {name for name in os.environ if name.startswith("WISPR_")}

    assert wispr == {"WISPR_ARCHIVE_DIR", "WISPR_DATA_DIR"}
    assert Path(os.environ["XDG_CONFIG_HOME"]).is_relative_to(tmp_path)
    assert Path.cwd() == tmp_path
    assert "HTTPS_PROXY" not in os.environ
    assert "SSL_CERT_FILE" not in os.environ
