"""Thin TCP client for the Dungeondraft MCP bridge mod.

Each request opens a short-lived connection, sends one newline-delimited JSON
object, and reads one newline-delimited JSON object back. See PROTOCOL.md.

Every request carries the bridge token: a secret the mod writes to
Dungeondraft's user folder on first start (`mcp_bridge_token`, next to
config.ini), so that only a program that can read the user's files can drive
the map. The file is read for every request, which is cheap and means a token
the user regenerates from the panel is picked up without a restart.
"""

from __future__ import annotations

import json
import os
import socket
import stat
from pathlib import Path

from .packs import data_dir

TOKEN_FILE = "mcp_bridge_token"


def token_path() -> Path:
    """Where the mod writes the token: Dungeondraft's user folder, or DD_BRIDGE_TOKEN_FILE."""
    override = os.environ.get("DD_BRIDGE_TOKEN_FILE")
    return Path(override) if override else data_dir() / TOKEN_FILE


def read_token() -> str:
    """The token the mod wrote, or "" when there is none (Dungeondraft has not
    started since the mod was installed, or an older mod). DD_BRIDGE_TOKEN
    overrides the file."""
    env = os.environ.get("DD_BRIDGE_TOKEN", "").strip()
    if env:
        return env
    path = token_path()
    try:
        token = path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    _keep_private(path)
    return token


def _keep_private(path: Path) -> None:
    """Godot's File cannot set permissions; make sure only the user can read the token."""
    if os.name == "nt":
        return
    try:
        if stat.S_IMODE(path.stat().st_mode) & 0o077:
            path.chmod(0o600)
    except OSError:
        pass


class BridgeError(Exception):
    """Raised when the bridge is unreachable or returns an error response."""


# Commands the mod answers over several frames, with the seconds to allow
# them. With the shared history a move goes through Dungeondraft's own
# History.Undo() / Redo(), which hold a lock for three frames after each step
# and wait out a save or an export, so an undo takes a few frames and a
# rollback a few frames per step. See PROTOCOL.md, "History".
SLOW_COMMANDS = {"undo": 20.0, "redo": 20.0, "rollback_checkpoint": 90.0}


class BridgeClient:
    def __init__(self, host: str = "127.0.0.1", port: int = 8787, timeout: float = 5.0):
        self.host = host
        self.port = port
        self.timeout = timeout

    def timeout_for(self, cmd: str) -> float:
        """Seconds to wait for the answer to `cmd`: never less than the client's own timeout."""
        return max(self.timeout, SLOW_COMMANDS.get(cmd, 0.0))

    def request(self, cmd: str, **params) -> dict:
        payload = {"cmd": cmd, **params}
        token = read_token()
        if token:
            payload["token"] = token
        data = (json.dumps(payload) + "\n").encode("utf-8")
        timeout = self.timeout_for(cmd)

        try:
            with socket.create_connection((self.host, self.port), timeout=timeout) as sock:
                sock.settimeout(timeout)
                sock.sendall(data)
                buf = b""
                while b"\n" not in buf:
                    chunk = sock.recv(4096)
                    if not chunk:
                        break
                    buf += chunk
        except (ConnectionRefusedError, OSError) as exc:
            raise BridgeError(
                f"Could not reach the Dungeondraft MCP bridge on {self.host}:{self.port}. "
                "Is Dungeondraft running with the MCP Bridge mod enabled and a map open? "
                f"({exc})"
            ) from exc

        line, _, _ = buf.partition(b"\n")
        if not line.strip():
            raise BridgeError("empty response from bridge")

        resp = json.loads(line.decode("utf-8"))
        if not resp.get("ok"):
            error = str(resp.get("error", "unknown bridge error"))
            if error.startswith(("bad token", "this bridge requires a token")):
                where = "DD_BRIDGE_TOKEN" if os.environ.get("DD_BRIDGE_TOKEN") else str(token_path())
                error += (f" (this server {'read' if token else 'found no'} token at {where}; "
                          "DD_BRIDGE_TOKEN_FILE or DD_BRIDGE_TOKEN override it)")
            raise BridgeError(error)
        return resp.get("result", {})
