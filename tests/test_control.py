"""Night mode control logic: ChargerControl with a fake runner, HelperRunner
with a fake helper process."""

import os
import sys
import tempfile
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(HERE, "..", "ext", "velib_python"))

from victronble import control as ctl  # noqa: E402

MAC = "C0:FF:EE:00:00:03"
OFF = {"ok": True, "night": False, "mode": 0, "max_current": 30.0}
ON = {"ok": True, "night": True, "mode": 2, "max_current": 15.0}


class FakeRunner:
    def __init__(self):
        self.jobs = []

    def submit(self, mac, op, callback):
        self.jobs.append([mac, op, callback])

    def finish(self, result):
        mac, op, callback = self.jobs.pop(0)
        callback(dict(result))
        return op


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class ChargerControlTest(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        p = mock.patch.object(ctl.time, "monotonic", self.clock)
        p.start()
        self.addCleanup(p.stop)
        self.runner = FakeRunner()
        self.changes = []
        self.c = ctl.ChargerControl(
            MAC, self.runner, self.clock.now, poll_interval=900, on_change=self.changes.append
        )

    def advance(self, seconds):
        self.clock.now += seconds
        self.c.tick(self.clock.now)

    def test_first_read_and_poll(self):
        self.c.tick(self.clock.now)
        self.assertEqual(self.runner.jobs, [])
        self.advance(ctl.FIRST_READ_DELAY)
        self.assertEqual(self.runner.jobs[0][1], "read")
        self.advance(10)
        self.assertEqual(len(self.runner.jobs), 1, "no second session while one runs")
        self.runner.finish(OFF)
        self.assertEqual((self.c.night, self.c.state, self.c.status), (False, False, ctl.STATUS_OFF))
        self.assertEqual(len(self.changes), 1)
        self.advance(899)
        self.assertEqual(self.runner.jobs, [])
        self.advance(1)
        self.assertEqual(self.runner.jobs[0][1], "read")

    def test_switch(self):
        self.advance(ctl.FIRST_READ_DELAY)
        self.runner.finish(OFF)
        self.c.request(True, self.clock.now)
        self.assertEqual(self.runner.jobs[0][1], "night-on")
        self.assertEqual(
            (self.c.state, self.c.status), (True, ctl.STATUS_ON), "shows the request while pending"
        )
        self.runner.finish(ON)
        self.assertEqual((self.c.night, self.c.requested, self.c.status), (True, None, ctl.STATUS_ON))
        self.assertEqual(self.c.max_current, 15.0)
        self.c.request(True, self.clock.now)
        self.assertEqual(self.runner.jobs, [], "already on: no session")
        self.c.request(False, self.clock.now)
        self.assertEqual(self.runner.finish(OFF), "night-off")
        self.assertFalse(self.c.night)

    def test_request_during_read(self):
        self.advance(ctl.FIRST_READ_DELAY)
        self.c.request(True, self.clock.now)
        self.assertEqual([j[1] for j in self.runner.jobs], ["read"])
        self.runner.finish(OFF)
        self.assertEqual([j[1] for j in self.runner.jobs], ["night-on"], "switch right after the read")

    def test_failed_switch_is_not_retried(self):
        self.advance(ctl.FIRST_READ_DELAY)
        self.runner.finish(OFF)
        self.c.request(True, self.clock.now)
        self.runner.finish({"ok": False, "error": "charger not reachable"})
        self.assertEqual((self.c.state, self.c.status), (False, ctl.STATUS_FAULT))
        self.assertEqual(self.c.error, "charger not reachable")
        self.assertEqual(self.runner.jobs, [])
        self.advance(ctl.RETRY_MIN)
        self.assertEqual(self.runner.jobs[0][1], "read", "a read retries after a minute")
        self.runner.finish({"ok": False, "error": "again"})
        self.advance(ctl.RETRY_MIN)
        self.assertEqual(self.runner.jobs, [], "backoff doubles")
        self.advance(ctl.RETRY_MIN)
        self.runner.finish(OFF)
        self.assertEqual((self.c.error, self.c.status), (None, ctl.STATUS_OFF))

    def test_unsupported(self):
        self.advance(ctl.FIRST_READ_DELAY)
        self.runner.finish({"ok": False, "unsupported": True, "error": "no night mode register"})
        self.assertTrue(self.c.unsupported)
        self.advance(10000)
        self.c.request(True, self.clock.now)
        self.assertEqual(self.runner.jobs, [])

    def test_closed(self):
        self.advance(ctl.FIRST_READ_DELAY)
        self.c.close()
        self.runner.finish(ON)
        self.assertIsNone(self.c.night)
        self.assertEqual(self.changes, [])
        self.advance(10000)
        self.assertEqual(self.runner.jobs, [])

    def test_info(self):
        self.advance(ctl.FIRST_READ_DELAY)
        self.assertTrue(self.c.info(self.clock.now)["busy"])
        self.runner.finish(ON)
        info = self.c.info(self.clock.now + 30)
        self.assertEqual((info["night"], info["busy"], info["last_ok_ago"]), (True, False, 30))
        self.assertIsNotNone(info["last_done"])


FAKE_HELPER = """
import json, sys, time
args = dict(zip(sys.argv[1::2], sys.argv[2::2]))
if args["--mac"].endswith("99"):
    time.sleep(30)
print("log line, not json")
print(json.dumps({"ok": True, "op": args["--op"], "mac": args["--mac"], "pins": args["--pins-file"]}))
"""


class HelperRunnerTest(unittest.TestCase):
    def setUp(self):
        from gi.repository import GLib

        self.GLib = GLib
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        helper = os.path.join(self.tmp.name, "helper.py")
        with open(helper, "w") as f:
            f.write(FAKE_HELPER)
        self.runner = ctl.HelperRunner(GLib, [sys.executable, helper], "/x/pins.json", timeout=2)

    def run_loop(self, until, timeout=20):
        ctx = self.GLib.MainContext.default()
        end = time.monotonic() + timeout
        while not until() and time.monotonic() < end:
            ctx.iteration(False)
            time.sleep(0.01)

    def test_sequential_sessions(self):
        results = []
        self.runner.submit("C0:FF:EE:00:00:01", "read", results.append)
        self.runner.submit("C0:FF:EE:00:00:02", "read", results.append)
        self.runner.submit("C0:FF:EE:00:00:02", "night-on", results.append)  # replaces the queued read
        self.runner.submit("C0:FF:EE:00:00:02", "read", results.append)  # does not replace a switch
        self.assertTrue(self.runner.busy())
        self.run_loop(lambda: len(results) == 2)
        self.assertEqual(
            [(r["mac"], r["op"]) for r in results],
            [("C0:FF:EE:00:00:01", "read"), ("C0:FF:EE:00:00:02", "night-on")],
        )
        self.assertEqual(results[0]["pins"], "/x/pins.json")
        self.run_loop(lambda: not self.runner.busy(), timeout=5)
        self.assertFalse(self.runner.busy())

    def test_timeout(self):
        results = []
        self.runner.submit("C0:FF:EE:00:00:99", "read", results.append)
        self.run_loop(lambda: results)
        self.assertFalse(results[0]["ok"])
        self.assertIn("without a result", results[0]["error"])

    def test_spawn_failure(self):
        runner = ctl.HelperRunner(self.GLib, ["/nonexistent/helper"], "/x/pins.json")
        results = []
        runner.submit(MAC, "read", results.append)
        self.run_loop(lambda: results)
        self.assertFalse(results[0]["ok"])
        self.assertIn("cannot start", results[0]["error"])


if __name__ == "__main__":
    unittest.main()
