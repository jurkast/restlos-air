"""Focused network status, worker-lifecycle and argument-safety checks."""
import importlib.util
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch


MODULE = Path(__file__).resolve().parents[1] / "app/network.py"
spec = importlib.util.spec_from_file_location("restlos_network", MODULE)
network = importlib.util.module_from_spec(spec)
spec.loader.exec_module(network)


class NetworkTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.sys_root = Path(self.temp.name)
        interface = self.sys_root / "wlp6s0"
        interface.mkdir()
        (interface / "operstate").write_text("up\n")
        (interface / "wireless").mkdir()
        self.route = {"interface": "wlp6s0", "gateway": "192.168.178.1",
                      "connection": "wlan", "metric": 600}

    def tearDown(self):
        self.temp.cleanup()

    def test_command_options_and_injection_rejected(self):
        for target in ["-f", "--help", "1.1.1.1;whoami", "$(id)", "foo\nbar", "",
                       "a..example", "-x.example", "example.org/", None]:
            with self.subTest(target=target), self.assertRaises(ValueError):
                network.validate_target(target)
        for target in ["1.1.1.1", "::1", "example.org", "a-b.example.org."]:
            self.assertEqual(network.validate_target(target), target)

    def test_probe_is_bounded_and_does_not_use_shell(self):
        completed = subprocess.CompletedProcess([], 0, "64 bytes: time=4.65 ms", "")
        with patch.object(network.subprocess, "run", return_value=completed) as run:
            self.assertEqual(network.ping_once("1.1.1.1", "/usr/bin/ping"), (4.65, "ok", ""))
        args, kwargs = run.call_args
        self.assertEqual(args[0][-1], "1.1.1.1")
        self.assertLess(kwargs["timeout"], 2)
        self.assertNotIn("shell", kwargs)
        self.assertEqual(kwargs["env"]["LC_ALL"], "C")

    def test_filtered_icmp_is_not_reported_as_disconnected(self):
        monitor = network.NetworkMonitor(sys_root=self.sys_root)
        with patch.object(network, "discover_route", return_value=self.route), \
                patch.object(network, "ping_once", return_value=(None, "no_reply", "ICMP fehlt")):
            result = monitor._sample()
        self.assertEqual(result["status"], "no_reply")
        self.assertEqual(result["connection"], "wlan")
        self.assertEqual(result["loss_percent"], 100)
        self.assertEqual(result["samples"], 1)

    def test_missing_route_and_disconnected_interface_skip_probes(self):
        monitor = network.NetworkMonitor(sys_root=self.sys_root)
        with patch.object(network, "discover_route", return_value=None), \
                patch.object(network, "ping_once") as ping:
            self.assertEqual(monitor._sample()["status"], "no_route")
            ping.assert_not_called()
        (self.sys_root / "wlp6s0/operstate").write_text("down\n")
        monitor._route_at = 0
        with patch.object(network, "discover_route", return_value=self.route), \
                patch.object(network, "ping_once") as ping:
            self.assertEqual(monitor._sample()["status"], "disconnected")
            ping.assert_not_called()

    def test_route_cached_and_probe_history_bounded(self):
        monitor = network.NetworkMonitor(sys_root=self.sys_root)
        answers = [(1.0, "ok", ""), (None, "no_reply", "")] * 18
        with patch.object(network, "discover_route", return_value=self.route) as discover, \
                patch.object(network, "ping_once", side_effect=answers):
            for _ in range(18):
                result = monitor._sample()
        self.assertEqual(discover.call_count, 1)
        self.assertEqual(result["samples"], 12)
        self.assertEqual(result["loss_percent"], 100)

    def test_route_change_resets_previous_losses(self):
        monitor = network.NetworkMonitor(sys_root=self.sys_root)
        monitor._results.extend([False] * 12)
        monitor._route = {**self.route, "gateway": "192.168.1.1"}
        with patch.object(network, "discover_route", return_value=self.route), \
                patch.object(network, "ping_once", return_value=(2.1, "ok", "")):
            result = monitor._sample()
        self.assertEqual(result["samples"], 1)
        self.assertEqual(result["loss_percent"], 0)

    def test_worker_start_is_idempotent_and_stop_wakes_sleep(self):
        event = threading.Event()
        monitor = network.NetworkMonitor(callback=lambda result: event.set(), interval=20)
        with patch.object(monitor, "_sample", return_value=monitor._empty("ok", "")):
            monitor.start()
            worker = monitor._thread
            self.assertTrue(event.wait(1))
            monitor.start()
            self.assertIs(monitor._thread, worker)
            monitor.stop(join_timeout=1)
            self.assertFalse(worker.is_alive())

    def test_missing_ping_permissions_are_unavailable_not_loss(self):
        completed = subprocess.CompletedProcess([], 2, "", "ping: socket: Operation not permitted")
        with patch.object(network.subprocess, "run", return_value=completed):
            self.assertEqual(network.ping_once("1.1.1.1", "/usr/bin/ping")[1], "unavailable")

    def test_proc_route_fallback_and_wlan_detection(self):
        route_file = self.sys_root / "routes"
        route_file.write_text("Iface Destination Gateway Flags RefCnt Use Metric Mask\n"
                              "wlp6s0 00000000 01B2A8C0 0003 0 0 600 00000000\n")
        with patch.object(network.shutil, "which", return_value=None):
            route = network.discover_route(self.sys_root, route_file)
        self.assertEqual(route["gateway"], "192.168.178.1")
        self.assertEqual(route["connection"], "wlan")


if __name__ == "__main__":
    unittest.main()
