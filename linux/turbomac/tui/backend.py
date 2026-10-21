"""Where turbomactop gets its data: the Backend interface and the live socket backend.

The TUI never touches sysfs, the SMC selector or RAPL. LiveBackend talks to
turbomacd over the control socket; MockBackend (mock.py) simulates jia-mac-pro;
ReplayBackend (replay.py) plays a turbomacd sample log. All three return the
shapes of turbomacd's replies (main.Daemon.build_status, dispatch).
"""
from __future__ import annotations

import abc
import threading
from collections import deque
from typing import Any, Callable, Iterator

from turbomac import config
from turbomac.protocol import Client, ProtocolError

StatusCallback = Callable[[dict[str, Any]], None]
ChangeCallback = Callable[[bool, "str | None"], None]
BACKOFF_S = (0.5, 1.0, 2.0, 4.0, 5.0)


class BackendError(Exception):
    """A request failed: daemon unreachable, request rejected, or a bad reply."""


class Subscription:
    """Worker thread delivering status dicts (~4/s) to a callback; reconnects with backoff.

    The callback runs on this thread (the app forwards it with call_from_thread).
    If the callback raises, the subscription ends (the app is going away).
    """

    def __init__(self, backend: "Backend", callback: StatusCallback, on_change: ChangeCallback | None) -> None:
        self.backend, self.callback, self.on_change = backend, callback, on_change
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._run, name="turbomactop-subscribe", daemon=True)

    def start(self) -> "Subscription":
        self.thread.start()
        return self

    def stop(self) -> None:
        self.stop_event.set()

    def _conn(self, ok: bool, err: str | None) -> None:
        changed = ok != self.backend.connected or (not ok and err != self.backend.last_error)
        self.backend.connected = ok
        if not ok:
            self.backend.last_error = err
        if changed and self.on_change is not None and not self.stop_event.is_set():
            try:
                self.on_change(ok, err)
            except Exception:  # noqa: BLE001 - the app is going away
                self.stop_event.set()

    def _run(self) -> None:
        attempt = 0
        while not self.stop_event.is_set():
            try:
                for status in self.backend._stream(self.stop_event):
                    if self.stop_event.is_set():
                        return
                    attempt = 0
                    self._conn(True, None)
                    try:
                        self.callback(status)
                    except Exception:  # noqa: BLE001 - the app is going away
                        self.stop_event.set()
                        return
                raise BackendError("status stream ended")
            except Exception as exc:  # noqa: BLE001 - any failure means: back off and reconnect
                if self.stop_event.is_set():
                    return
                self._conn(False, str(exc))
                self.stop_event.wait(BACKOFF_S[min(attempt, len(BACKOFF_S) - 1)])
                attempt += 1


class Backend(abc.ABC):
    """What the TUI may ask for. Every method may raise BackendError."""

    kind = "?"
    period_s = 0.25

    def __init__(self) -> None:
        self.connected = False
        self.last_error: str | None = None

    @property
    def label(self) -> str:
        return self.kind

    @abc.abstractmethod
    def status(self) -> dict[str, Any]: ...
    @abc.abstractmethod
    def history(self, seconds: float = 600, full: bool = False) -> list[dict[str, Any]]: ...
    @abc.abstractmethod
    def config_get(self) -> dict[str, Any]: ...
    @abc.abstractmethod
    def config_set(self, updates: dict[str, Any]) -> dict[str, Any]: ...
    @abc.abstractmethod
    def config_reset(self) -> dict[str, Any]: ...
    @abc.abstractmethod
    def gpu_set(self, mode: str) -> dict[str, Any]: ...
    @abc.abstractmethod
    def incidents(self) -> list[dict[str, Any]]: ...
    @abc.abstractmethod
    def incident(self, incident_id: str) -> dict[str, Any]: ...
    @abc.abstractmethod
    def events(self, n: int = 100) -> list[dict[str, Any]]: ...
    @abc.abstractmethod
    def smc_keys(self) -> list[dict[str, Any]]: ...

    def os_name(self) -> str | None:
        return None

    def subscribe(self, callback: StatusCallback, on_change: ChangeCallback | None = None) -> Subscription:
        return Subscription(self, callback, on_change).start()

    def _stream(self, stop: threading.Event) -> Iterator[dict[str, Any]]:
        """Default stream: poll status() every period_s (simulated backends)."""
        while not stop.is_set():
            yield self.status()
            stop.wait(self.period_s)

    def close(self) -> None:
        pass


def read_os_release(path: str = "/etc/os-release") -> str | None:
    """'Ubuntu 26.04' from os-release (the socket is local, so this is the daemon's host)."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            kv = dict(line.rstrip("\n").split("=", 1) for line in fh if "=" in line)
    except OSError:
        return None
    name, ver = kv.get("NAME", "").strip('"'), kv.get("VERSION_ID", "").strip('"')
    return f"{name} {ver}".strip() or None


class LiveBackend(Backend):
    """turbomacd over its Unix socket (PLAN 4.6)."""

    kind = "live"

    def __init__(self, socket_path: str | None = None, timeout: float = 10.0) -> None:
        super().__init__()
        self.client = Client(socket_path, timeout=timeout)

    @property
    def label(self) -> str:
        return f"live {self.client.path}"

    def _call(self, cmd: str, timeout: float | None = None, **args: Any) -> Any:
        try:
            return self.client.call(cmd, timeout=timeout, **args)
        except (ProtocolError, OSError, ValueError) as exc:
            raise BackendError(str(exc)) from exc

    def status(self) -> dict[str, Any]:
        return self._call("status")

    def history(self, seconds: float = 600, full: bool = False) -> list[dict[str, Any]]:
        return self._call("history", seconds=seconds, full=full)

    def config_get(self) -> dict[str, Any]:
        return self._call("config.get")

    def config_set(self, updates: dict[str, Any]) -> dict[str, Any]:
        return self._call("config.set", timeout=60, **updates)

    def config_reset(self) -> dict[str, Any]:
        return self._call("config.reset", timeout=60)

    def gpu_set(self, mode: str) -> dict[str, Any]:
        return self._call("gpu.set", timeout=90, mode=mode)

    def incidents(self) -> list[dict[str, Any]]:
        return self._call("incidents")

    def incident(self, incident_id: str) -> dict[str, Any]:
        return self._call("incident", id=incident_id)

    def events(self, n: int = 100) -> list[dict[str, Any]]:
        return self._call("events", n=n)

    def smc_keys(self) -> list[dict[str, Any]]:
        return self._call("smc.keys")

    def os_name(self) -> str | None:
        return read_os_release()

    def _stream(self, stop: threading.Event) -> Iterator[dict[str, Any]]:
        try:
            yield from self.client.stream("subscribe", timeout=5.0)
        except (ProtocolError, OSError, ValueError) as exc:
            raise BackendError(str(exc)) from exc


class SimBackend(Backend):
    """Shared by the mock and replay backends: in-memory config with the daemon's
    own validation (config.validate), an event list, and Daemon.set_config's reply shape."""

    persisted = True
    not_persisted_reason: str | None = None
    config_path = "(simulated)"

    def __init__(self, cfg: dict[str, Any] | None = None) -> None:
        super().__init__()
        self.lock = threading.RLock()
        self.config = cfg or config.defaults()
        self.event_log: deque[dict[str, Any]] = deque(maxlen=1000)
        self.last_change_ts: float | None = None

    def wall(self) -> float:
        raise NotImplementedError

    def emit(self, typ: str, **fields: Any) -> dict[str, Any]:
        rec = {"ts": round(self.wall(), 3), "type": typ, **fields}
        self.event_log.append(rec)
        return rec

    def events(self, n: int = 100) -> list[dict[str, Any]]:
        with self.lock:
            return list(self.event_log)[-max(0, n):] if n else []

    def config_get(self) -> dict[str, Any]:
        with self.lock:
            return {"config": config.apply_updates(self.config, {}), "defaults": config.defaults(),
                    "digest": config.digest(self.config), "path": self.config_path,
                    "hard_max_w": config.HARD_MAX_W}

    def on_config(self, accepted: dict[str, Any]) -> None:
        """Hook: react to accepted keys (simulation side effects)."""

    def config_set(self, updates: dict[str, Any]) -> dict[str, Any]:
        updates = dict(updates)
        gpu_mode = updates.pop("gpu.mode", None)
        with self.lock:
            accepted, rejected = config.validate(updates, self.config)
            if accepted:
                self.config = config.apply_updates(self.config, accepted)
                self.last_change_ts = self.wall()
                self.emit("config_change", accepted=accepted, rejected=rejected, persisted=self.persisted)
                self.on_config(accepted)
            out: dict[str, Any] = {"accepted": accepted, "rejected": rejected, "persisted": self.persisted,
                                   "error": self.not_persisted_reason, "digest": config.digest(self.config)}
        if gpu_mode is not None and gpu_mode == self.config["gpu"]["mode"]:
            out["accepted"]["gpu.mode"] = gpu_mode
        elif gpu_mode is not None:
            try:
                out["gpu"] = self.gpu_set(gpu_mode)
                out["accepted"]["gpu.mode"] = out["gpu"]["mode"]
            except BackendError as exc:
                out["rejected"]["gpu.mode"] = str(exc)
        return out

    def config_reset(self) -> dict[str, Any]:
        return self.config_set({k: v for k, v in config.flatten(config.DEFAULTS).items() if k != "schema"})
