"""turbomactop under Textual's pilot: screens, keys, settings round trip, the GPU off gate, replay, narrow layout."""
import importlib.util
import os
import unittest

import helpers

HAVE_TEXTUAL = importlib.util.find_spec("textual") is not None
FIXTURE = os.path.join(helpers.ROOT, "fixtures", "samples-oldmac-20261009.jsonl.gz")


def recording(backend, name):
    """Wrap backend.<name> so calls are recorded."""
    calls, real = [], getattr(backend, name)

    def wrapper(*a, **k):
        calls.append(a[0] if a else k)
        return real(*a, **k)
    setattr(backend, name, wrapper)
    return calls


@unittest.skipUnless(HAVE_TEXTUAL, "textual not installed (TUI venv)")
class AppTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from turbomac.tui.app import TurboMacTop
        from turbomac.tui.mock import MockBackend
        self.App, self.Mock = TurboMacTop, MockBackend

    def mock(self):
        return self.Mock(seed=1, warmup_s=120, gpu_delay_s=0)

    async def settle(self, app, pilot, delay=0.3):
        await pilot.pause(delay)
        await app.workers.wait_for_complete()
        await pilot.pause(0.1)

    async def test_every_screen_mounts_and_keys_switch(self):
        from textual.widgets import DataTable, RichLog, TabbedContent
        from turbomac.tui.app import VIEWS
        from turbomac.tui.widgets import HelpScreen
        app = self.App(self.mock())
        async with app.run_test(size=(120, 36)) as pilot:
            await self.settle(app, pilot, 0.6)
            tabs = app.query_one(TabbedContent)
            for key, tab in (("2", "settings"), ("3", "gpu"), ("4", "incidents"), ("5", "log"), ("1", "dash")):
                await pilot.press(key)
                await self.settle(app, pilot)
                self.assertEqual(tabs.active, tab)
                self.assertTrue(app.query_one(VIEWS[tab]).display)
            self.assertEqual(app.query_one(DataTable).row_count, 2)
            self.assertGreater(len(app.query_one(RichLog).lines), 5)
            gov = str(app.query_one("#gov-panel").render())
            self.assertIn(app.status["governor"]["state"], gov)
            self.assertIn("ACPW", str(app.query_one("#input-panel").render()))
            self.assertIn("incident", str(app.query_one("#banner").render()))
            before = app.status["ts"]
            await pilot.press("r")
            self.assertTrue(app.peaks_since is None or app.peaks_since >= before)
            await pilot.press("p")
            self.assertTrue(app.paused)
            await pilot.press("p")
            self.assertFalse(app.paused)
            await pilot.press("question_mark")
            await pilot.pause(0.1)
            self.assertIsInstance(app.screen, HelpScreen)
            await pilot.press("x")
            await pilot.pause(0.1)
            self.assertNotIsInstance(app.screen, HelpScreen)
            await pilot.click("#tabs Tab#--content-tab-gpu")
            await self.settle(app, pilot)
            self.assertEqual(tabs.active, "gpu")
            self.assertEqual(app.render_errors, [])

    async def test_settings_edit_goes_through_config_set(self):
        from textual.widgets import Input, Select
        from turbomac.tui.widgets import ConfirmScreen
        be = self.mock()
        calls = recording(be, "config_set")
        app = self.App(be)
        async with app.run_test(size=(120, 36)) as pilot:
            await pilot.press("2")
            await self.settle(app, pilot, 0.5)
            self.assertEqual(calls, [])           # loading the editors never applies anything
            ceiling = app.query_one("#ed-governor--ceiling_w", Input)
            self.assertEqual(ceiling.value, "40")
            ceiling.focus()
            ceiling.value = "38"
            await pilot.press("enter")
            await self.settle(app, pilot)
            self.assertEqual(calls[-1], {"governor.ceiling_w": 38})
            self.assertEqual(be.config["governor"]["ceiling_w"], 38)
            row = app.query_one("#row-governor--ceiling_w")
            self.assertIn("applied", str(row.query_one(".set-result").render()))
            self.assertIn("ceiling 38 W ≈ 68.0 W input", str(app.query_one("#cfg-preview").render()))
            ceiling.value = "50"
            await pilot.press("enter")
            await self.settle(app, pilot)
            self.assertIn("REJECTED: out of range 10..45", str(row.query_one(".set-result").render()))
            self.assertEqual(be.config["governor"]["ceiling_w"], 38)
            mode = app.query_one("#ed-governor--mode", Select)
            mode.focus()
            mode.value = "dynamic"
            await self.settle(app, pilot)
            self.assertEqual(calls[-1], {"governor.mode": "dynamic"})
            self.assertEqual(be.config["governor"]["mode"], "dynamic")
            await pilot.press("escape", "d")
            await pilot.pause(0.1)
            self.assertIsInstance(app.screen, ConfirmScreen)
            await pilot.press("y")
            await self.settle(app, pilot)
            self.assertEqual((be.config["governor"]["mode"], be.config["governor"]["ceiling_w"]), ("static", 40))
            self.assertEqual(app.render_errors, [])

    async def test_gpu_off_needs_the_word_off(self):
        from textual.widgets import RadioButton
        from turbomac.tui.widgets import GpuOffScreen
        be = self.mock()
        calls = recording(be, "gpu_set")
        app = self.App(be)
        async with app.run_test(size=(120, 36)) as pilot:
            await pilot.press("3")
            await self.settle(app, pilot)
            self.assertTrue(app.query_one("#gm-auto", RadioButton).value)
            await pilot.click("#gm-off")
            await pilot.pause(0.2)
            self.assertIsInstance(app.screen, GpuOffScreen)
            await pilot.press("o", "f", "f", "enter")             # lower case is not OFF
            await pilot.pause(0.1)
            self.assertIsInstance(app.screen, GpuOffScreen)
            self.assertIn("type OFF exactly", str(app.screen.query_one("#gpu-off-error").render()))
            await pilot.click("#confirm")
            await pilot.pause(0.1)
            self.assertIsInstance(app.screen, GpuOffScreen)
            await pilot.press("escape")
            await self.settle(app, pilot)
            self.assertNotIsInstance(app.screen, GpuOffScreen)
            self.assertEqual(calls, [])
            self.assertEqual(be.sim.gpu_mode, "auto")
            self.assertTrue(app.query_one("#gm-auto", RadioButton).value)
            await pilot.click("#gm-low")                           # low needs no confirmation
            await self.settle(app, pilot, 0.5)
            self.assertEqual(calls, ["low"])
            await pilot.click("#gm-off")
            await pilot.pause(0.2)
            await pilot.press("O", "F", "F", "enter")
            await self.settle(app, pilot, 0.5)
            self.assertEqual(calls, ["low", "off"])
            self.assertEqual(be.sim.gpu_mode, "off")
            self.assertIn("switcheroo OFF", str(app.query_one("#gpu-steps").render()))
            self.assertEqual(app.render_errors, [])

    async def test_replay_backed_run_renders_without_exceptions(self):
        from turbomac.tui.replay import ReplayBackend
        app = self.App(ReplayBackend(FIXTURE, speed=20))
        async with app.run_test(size=(120, 36)) as pilot:
            for key in "123451":
                await pilot.press(key)
                await self.settle(app, pilot, 0.4)
            await pilot.resize_terminal(80, 24)
            await self.settle(app, pilot)
            self.assertEqual(app.status["host"], "replay")
            self.assertGreater(len(app.slow["input"]), 5)
            self.assertEqual(app.render_errors, [])

    async def test_narrow_terminal_hides_rails_and_top_first(self):
        app = self.App(self.mock())
        async with app.run_test(size=(80, 24)) as pilot:
            await self.settle(app, pilot)
            self.assertTrue(app.query_one("#tabs").has_class("-narrow"))
            for sel in ("#rails-panel", "#top-panel", "#gpu-panel", ".spark-long"):
                self.assertFalse(app.query_one(sel).display, sel)
            for sel in ("#cpu-panel", "#gov-panel", "#gpu-panel-narrow", "#fans-panel"):
                self.assertTrue(app.query_one(sel).display, sel)
            await pilot.resize_terminal(120, 36)
            await self.settle(app, pilot)
            self.assertTrue(app.query_one("#rails-panel").display)
            self.assertEqual(app.render_errors, [])

    async def test_live_settings_change_reaches_disk_and_fake_sysfs(self):
        """The real turbomacd run loop on a fake sysroot, the TUI over its socket."""
        import io
        import tempfile
        import threading
        from textual.widgets import Input
        from turbomac import config
        from turbomac.daemon.logger import EventLog
        from turbomac.daemon.paths import Paths
        from turbomac.tui.backend import LiveBackend
        fs, tmp = helpers.FakeSys(), tempfile.TemporaryDirectory()
        helpers.add_amdgpu(fs.root)
        paths = Paths(sysroot=fs.root, socket=os.path.join(tmp.name, "t.sock"))
        d = helpers.make_daemon(paths, EventLog(stream=io.StringIO()))
        d.setup(start_server=True)
        loop = threading.Thread(target=d.run, daemon=True)
        loop.start()
        try:
            app = self.App(LiveBackend(paths.socket))
            async with app.run_test(size=(120, 36)) as pilot:
                await self.settle(app, pilot, 0.8)
                self.assertTrue(app.backend.connected)
                self.assertEqual(app.status["governor"]["state"], "STATIC")
                await pilot.press("2")
                await self.settle(app, pilot, 0.5)
                cap = app.query_one("#ed-governor--static_cap_w", Input)
                cap.focus()
                cap.value = "30"
                await pilot.press("enter")
                await self.settle(app, pilot, 0.8)
                self.assertEqual(config.load(paths.config)[0]["governor"]["static_cap_w"], 30)
                for _ in range(20):           # the static loop re-asserts PL1 = PL2 = 30 W
                    if helpers.read(os.path.join(fs.zone, "constraint_0_power_limit_uw")) == "30000000":
                        break
                    await pilot.pause(0.1)
                self.assertEqual(helpers.read(os.path.join(fs.zone, "constraint_0_power_limit_uw")), "30000000")
                for key in "345":
                    await pilot.press(key)
                    await self.settle(app, pilot)
                self.assertEqual(app.render_errors, [])
        finally:
            d.stop_event.set()
            loop.join(5)
            d.shutdown()
            fs.cleanup()
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
