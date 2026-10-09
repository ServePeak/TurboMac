"""python3 -m turbomac.tui [--socket PATH] [--mock [--seed N]] [--replay FILE [--speed N]]"""
from __future__ import annotations

import argparse
import sys

from turbomac.protocol import default_socket


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="turbomactop", description="Full-screen TUI for turbomacd (TurboMac)")
    ap.add_argument("--socket", help=f"control socket (default {default_socket()})")
    src = ap.add_mutually_exclusive_group()
    src.add_argument("--mock", action="store_true", help="simulated old-mac, no daemon needed")
    src.add_argument("--replay", metavar="FILE", help="play a turbomacd sample log (.jsonl or .jsonl.gz)")
    ap.add_argument("--seed", type=int, help="mock: random seed (default 1)")
    ap.add_argument("--speed", type=float, help="replay: records per second (default 1)")
    a = ap.parse_args(argv)
    if a.seed is not None and not a.mock:
        ap.error("--seed needs --mock")
    if a.speed is not None and not a.replay:
        ap.error("--speed needs --replay")
    try:
        from turbomac.tui.app import TurboMacTop
        from turbomac.tui.backend import BackendError, LiveBackend
    except ImportError as exc:
        print(f"turbomactop: {exc}. Install the TUI venv: pip install -r requirements-tui.txt", file=sys.stderr)
        return 1
    try:
        if a.mock:
            from turbomac.tui.mock import MockBackend
            backend = MockBackend(seed=1 if a.seed is None else a.seed)
        elif a.replay:
            from turbomac.tui.replay import ReplayBackend
            backend = ReplayBackend(a.replay, speed=a.speed or 1.0)
        else:
            backend = LiveBackend(a.socket)
    except (BackendError, OSError, ValueError) as exc:
        print(f"turbomactop: {exc}", file=sys.stderr)
        return 1
    TurboMacTop(backend).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
