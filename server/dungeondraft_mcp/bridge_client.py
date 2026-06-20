"""Thin TCP client for the Dungeondraft MCP bridge mod.

Each request opens a short-lived connection, sends one newline-delimited JSON
object, and reads one newline-delimited JSON object back. See PROTOCOL.md.
"""

from __future__ import annotations

import json
import socket


class BridgeError(Exception):
    """Raised when the bridge is unreachable or returns an error response."""


class BridgeClient:
    def __init__(self, host: str = "127.0.0.1", port: int = 8787, timeout: float = 5.0):
        self.host = host
        self.port = port
        self.timeout = timeout

    def request(self, cmd: str, **params) -> dict:
        payload = {"cmd": cmd, **params}
        data = (json.dumps(payload) + "\n").encode("utf-8")

        try:
            with socket.create_connection((self.host, self.port), timeout=self.timeout) as sock:
                sock.settimeout(self.timeout)
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
            raise BridgeError(resp.get("error", "unknown bridge error"))
        return resp.get("result", {})
