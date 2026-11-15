"""hci.Listener with a SOCK_SEQPACKET socket pair standing in for the HCI socket."""

import os
import socket
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import advgen  # noqa: E402
from victronble import hci  # noqa: E402

MFG = bytes.fromhex("100289a302b040af925d09a4d89aa0128bdef48c6298a9")


class FakeGLib:
    PRIORITY_DEFAULT = 0
    IO_IN, IO_ERR, IO_HUP, IO_NVAL = 1, 8, 16, 32

    def __init__(self):
        self.watches = {}
        self.removed = []

    def io_add_watch(self, fd, priority, condition, callback, *data):
        source = len(self.watches) + 1
        self.watches[source] = (fd, callback, data)
        return source

    def source_remove(self, source):
        self.removed.append(source)


class ListenerTest(unittest.TestCase):
    def setUp(self):
        self.ours, self.kernel = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        self.ours.setblocking(False)
        self.addCleanup(self.kernel.close)
        self.glib = FakeGLib()
        self.received = []
        self.listener = hci.Listener(lambda *a: self.received.append(a), self.glib, scan_watchdog=60)
        fd = os.dup(self.ours.fileno())
        self.ours.close()
        patches = [
            mock.patch.object(hci, "list_controllers", lambda: [0]),
            mock.patch.object(hci, "open_raw_socket", lambda dev_id: fd),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.listener.refresh()
        self.addCleanup(self.listener.close)
        (self.source,) = self.glib.watches
        self.fd, self.callback, self.data = self.glib.watches[self.source]

    def readable(self, condition=FakeGLib.IO_IN):
        return self.callback(self.fd, condition, *self.data)

    def test_events(self):
        adv = advgen.advertisement(MFG)
        self.kernel.send(advgen.hci_event("C0:FF:EE:12:34:56", -70, adv))
        self.kernel.send(bytes([0x04, 0x0E, 4, 1, 0x0C, 0x20, 0]))  # not an LE meta event
        self.kernel.send(advgen.hci_event("C0:FF:EE:12:34:57", -80, adv, extended=True))
        self.assertTrue(self.readable())
        self.assertEqual(
            [r[:2] for r in self.received], [("C0:FF:EE:12:34:56", -70), ("C0:FF:EE:12:34:57", -80)]
        )
        self.assertEqual(self.listener.reports, 2)
        self.assertEqual(self.listener.controllers(), ["hci0"])

    def test_handler_exception_does_not_stop_listening(self):
        self.listener._callback = mock.Mock(side_effect=[ValueError("boom"), None])
        self.kernel.send(advgen.hci_event("C0:FF:EE:12:34:56", -70, advgen.advertisement(MFG)))
        self.kernel.send(advgen.hci_event("C0:FF:EE:12:34:56", -70, advgen.advertisement(MFG)))
        with self.assertLogs("hci", "ERROR"):
            self.assertTrue(self.readable())
        self.assertEqual(self.listener._callback.call_count, 2)

    def test_hangup_closes(self):
        self.kernel.close()
        with self.assertLogs("hci", "WARNING"):
            self.assertFalse(self.readable(FakeGLib.IO_IN | FakeGLib.IO_HUP))
        self.assertEqual(self.listener.controllers(), [])

    def test_scan_watchdog(self):
        self.listener.last_report = time.monotonic() - 61
        with self.assertLogs("hci", "INFO"):
            self.listener.refresh()
        self.assertEqual(self.kernel.recv(64), hci.LE_SET_SCAN_ENABLE)
        self.listener.refresh()  # not again right away
        self.kernel.setblocking(False)
        with self.assertRaises(BlockingIOError):
            self.kernel.recv(64)


if __name__ == "__main__":
    unittest.main()
