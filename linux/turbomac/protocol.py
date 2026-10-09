"""Newline-delimited JSON over the turbomacd Unix socket (PLAN 4.6).

Request: {"cmd": "config.set", "args": {...}, "id": optional}
Reply:   {"ok": true, "result": ..., "id": ...} or {"ok": false, "error": "...", "id": ...}
"""
from __future__ import annotations

import json
import os
import socket
from typing import Any, Iterator

DEFAULT_SOCKET = "/run/turbomac/turbomacd.sock"
MAX_REQUEST = 64 * 1024
COMMANDS = ("status", "history", "subscribe", "config.get", "config.set", "config.reset",
            "gpu.set", "incidents", "incident", "events", "smc.keys")


class ProtocolError(Exception):
    pass


def default_socket() -> str:
    return os.environ.get("TURBOMAC_SOCKET") or DEFAULT_SOCKET


def encode(obj: Any) -> bytes:
    return (json.dumps(obj, separators=(",", ":"), default=str) + "\n").encode("utf-8")


def decode(line: bytes) -> dict[str, Any]:
    obj = json.loads(line.decode("utf-8"))
    if not isinstance(obj, dict):
        raise ValueError("message must be a JSON object")
    return obj


def ok(result: Any, req_id: Any = None) -> dict[str, Any]:
    return {"ok": True, "result": result, "id": req_id}


def error(msg: str, req_id: Any = None) -> dict[str, Any]:
    return {"ok": False, "error": msg, "id": req_id}


class Client:
    def __init__(self, path: str | None = None, timeout: float = 15.0) -> None:
        self.path = path or default_socket()
        self.timeout = timeout

    def _connect(self, timeout: float | None) -> socket.socket:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(timeout if timeout is not None else self.timeout)
        try:
            s.connect(self.path)
        except OSError as exc:
            s.close()
            raise ProtocolError(f"cannot connect to {self.path}: {exc.strerror or exc}") from exc
        return s

    def stream(self, cmd: str, timeout: float | None = None, **args: Any) -> Iterator[dict[str, Any]]:
        """Yield every reply line until the server closes the connection."""
        s = self._connect(timeout)
        with s, s.makefile("rb") as fh:
            s.sendall(encode({"cmd": cmd, "args": args}))
            for line in fh:
                msg = decode(line)
                if not msg.get("ok"):
                    raise ProtocolError(msg.get("error", "request failed"))
                yield msg.get("result")

    def call(self, cmd: str, timeout: float | None = None, **args: Any) -> Any:
        for result in self.stream(cmd, timeout=timeout, **args):
            return result
        raise ProtocolError("connection closed without a reply")
