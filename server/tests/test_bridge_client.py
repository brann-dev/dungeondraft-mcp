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


if __name__ == "__main__":
    unittest.main()
