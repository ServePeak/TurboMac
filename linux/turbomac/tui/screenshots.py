"""Save SVG screenshots of every screen: python -m turbomac.tui.screenshots [OUTDIR]

Mock backend (seed 1, dynamic mode, mid GPU-loaded burst) for screens 1-5 at
120x36 and the dashboard at 80x24; replay backend (the old-mac fixture) for the
dashboard at 120x36. Default OUTDIR: linux/receipts/phase2-screens.
"""
from __future__ import annotations

import asyncio
import os
import sys

from turbomac.tui.app import TurboMacTop
from turbomac.tui.mock import MockBackend
from turbomac.tui.replay import ReplayBackend

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
FIXTURE = os.path.join(ROOT, "fixtures", "samples-oldmac-20261009.jsonl.gz")
SCREENS = (("1", "dashboard"), ("2", "settings"), ("3", "gpu"), ("4", "incidents"), ("5", "log"))


def mock() -> MockBackend:
    # 650 s in: the third GPU-loaded burst has been cut from the 40 W ceiling to 35 W (TRIM).
    return MockBackend(seed=1, warmup_s=650, config_updates={"governor.mode": "dynamic"}, gpu_delay_s=0)


async def settle(app: TurboMacTop, pilot, seconds: float = 0.6) -> None:
    await pilot.pause(seconds)
    await app.workers.wait_for_complete()
    await pilot.pause(0.2)


async def shoot(app: TurboMacTop, size: tuple[int, int], shots: list[tuple[str, str]], out: str) -> list[str]:
    paths = []
    async with app.run_test(size=size) as pilot:
        await settle(app, pilot, 1.0)
        for key, name in shots:
            await pilot.press(key)
            await settle(app, pilot)
            app.paused = True        # freeze the frame while it is saved
            paths.append(app.save_screenshot(f"{name}.svg", path=out))
            app.paused = False
        if app.render_errors:
            raise SystemExit(f"render errors: {app.render_errors}")
    return paths


async def main(out: str) -> list[str]:
    os.makedirs(out, exist_ok=True)
    paths = await shoot(TurboMacTop(mock()), (120, 36), [(k, f"mock-{i}-{n}") for i, (k, n) in
                                                          enumerate(SCREENS, 1)], out)
    paths += await shoot(TurboMacTop(mock()), (80, 24), [("1", "mock-1-dashboard-80x24")], out)
    replay = ReplayBackend(FIXTURE, speed=1.0)
    replay.advance(420)    # 7 minutes into the log, so the 10 min history has content
    paths += await shoot(TurboMacTop(replay), (120, 36), [("1", "replay-1-dashboard")], out)
    return paths


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, "receipts", "phase2-screens")
    for p in asyncio.run(main(target)):
        print(p)
