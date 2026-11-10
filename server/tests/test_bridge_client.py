"""The bridge client's token: where it comes from and that the file is kept private."""

import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from dungeondraft_mcp import bridge_client


class Token(unittest.TestCase):
    def test_env_token_wins(self):
        with mock.patch.dict(os.environ, {"DD_BRIDGE_TOKEN": " abc123 "}):
            self.assertEqual(bridge_client.read_token(), "abc123")

    def test_file_token_is_read_and_made_private(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "mcp_bridge_token"
            path.write_text("f" * 64 + "\n")
            path.chmod(0o644)
            with mock.patch.dict(os.environ, {"DD_BRIDGE_TOKEN_FILE": str(path), "DD_BRIDGE_TOKEN": ""}):
                self.assertEqual(bridge_client.read_token(), "f" * 64)
            if os.name != "nt":
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_missing_file_means_no_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"DD_BRIDGE_TOKEN_FILE": str(Path(tmp) / "none"), "DD_BRIDGE_TOKEN": ""}):
                self.assertEqual(bridge_client.read_token(), "")

    def test_default_path_is_in_dungeondrafts_user_folder(self):
        with mock.patch.dict(os.environ, {"DD_BRIDGE_TOKEN_FILE": ""}, clear=False):
            os.environ.pop("DD_BRIDGE_TOKEN_FILE", None)
            self.assertEqual(bridge_client.token_path().name, "mcp_bridge_token")
            self.assertEqual(bridge_client.token_path().parent, bridge_client.data_dir())


class Timeouts(unittest.TestCase):
    """History moves are answered over several frames, so they get longer to answer."""

    def test_history_moves_wait_longer(self):
        client = bridge_client.BridgeClient(timeout=5.0)
        self.assertEqual(client.timeout_for("get_status"), 5.0)
        for cmd in ("undo", "redo", "rollback_checkpoint"):
            self.assertGreater(client.timeout_for(cmd), 5.0, cmd)
        self.assertGreater(client.timeout_for("rollback_checkpoint"), client.timeout_for("undo"))

    def test_a_longer_client_timeout_is_kept(self):
        client = bridge_client.BridgeClient(timeout=500.0)
        self.assertEqual(client.timeout_for("undo"), 500.0)

    def test_the_request_uses_the_commands_timeout(self):
        client = bridge_client.BridgeClient(timeout=5.0)
        seen = {}

        class Sock:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def settimeout(self, value):
                seen["socket"] = value

            def sendall(self, data):
                seen["sent"] = data

            def recv(self, size):
                return b'{"ok": true, "result": {"undone": false}}\n'

        def connect(address, timeout):
            seen["connect"] = timeout
            return Sock()

        with mock.patch.object(bridge_client.socket, "create_connection", connect), \
                mock.patch.object(bridge_client, "read_token", return_value=""):
            self.assertEqual(client.request("undo", include_user=True), {"undone": False})
        self.assertEqual(seen["connect"], bridge_client.SLOW_COMMANDS["undo"])
        self.assertEqual(seen["socket"], bridge_client.SLOW_COMMANDS["undo"])
        self.assertIn(b'"include_user": true', seen["sent"])


if __name__ == "__main__":
    unittest.main()
