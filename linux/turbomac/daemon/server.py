"""Threaded Unix socket server for the control protocol."""
from __future__ import annotations

import grp
import os
import socket
import socketserver
import threading
import time
from typing import Any, Callable

from turbomac import protocol

SUBSCRIBE_PERIOD_S = 0.25
IDLE_TIMEOUT_S = 60.0
Handler = Callable[[str, dict[str, Any]], Any]


class _Conn(socketserver.StreamRequestHandler):
    server: "ControlServer"
    timeout = IDLE_TIMEOUT_S   # idle request connections are closed; also bounds a stalled subscriber

    def handle(self) -> None:
        while not self.server.stopping.is_set():
            try:
                line = self.rfile.readline(protocol.MAX_REQUEST + 1)
            except OSError:   # includes the idle timeout
                return
            if not line:
                return
            if len(line) > protocol.MAX_REQUEST or not line.endswith(b"\n"):
                self._send(protocol.error(f"request exceeds {protocol.MAX_REQUEST} bytes"))
                return
            try:
                req = protocol.decode(line)
            except ValueError as exc:
                self._send(protocol.error(f"bad request: {exc}"))
                continue
            cmd, args, rid = req.get("cmd"), req.get("args") or {}, req.get("id")
            if not isinstance(cmd, str) or not isinstance(args, dict):
                self._send(protocol.error("cmd must be a string and args an object", rid))
                continue
            if cmd == "subscribe":
                self._subscribe(rid)
                return
            try:
                self._send(protocol.ok(self.server.dispatch(cmd, args), rid))
            except (KeyError, ValueError, TypeError) as exc:
                self._send(protocol.error(str(exc), rid))
            except Exception as exc:  # noqa: BLE001 - never kill the server thread
                self._send(protocol.error(f"internal error: {exc!r}", rid))

    def _send(self, msg: dict[str, Any]) -> bool:
        try:
            self.wfile.write(protocol.encode(msg))
            self.wfile.flush()
            return True
        except OSError:
            return False

    def _subscribe(self, rid: Any) -> None:
        while not self.server.stopping.is_set():
            if not self._send(protocol.ok(self.server.dispatch("status", {}), rid)):
                return
            time.sleep(SUBSCRIBE_PERIOD_S)


class ControlServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, path: str, dispatch: Handler, group: str = "turbomac") -> None:
        self.dispatch = dispatch
        self.stopping = threading.Event()
        if os.path.dirname(path):
            os.makedirs(os.path.dirname(path), exist_ok=True)
        if os.path.exists(path):
            probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                probe.connect(path)
                raise RuntimeError(f"another turbomacd is listening on {path}")
            except OSError:
                os.unlink(path)
            finally:
                probe.close()
        old = os.umask(0o117)
        try:
            super().__init__(path, _Conn)
        finally:
            os.umask(old)
        os.chmod(path, 0o660)
        try:
            os.chown(path, -1, grp.getgrnam(group).gr_gid)
        except (KeyError, OSError):
            pass
        self.path = path
        self.thread = threading.Thread(target=self.serve_forever, name="control-socket", daemon=True)

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.stopping.set()
        self.shutdown()
        self.server_close()
        try:
            os.unlink(self.path)
        except OSError:
            pass
