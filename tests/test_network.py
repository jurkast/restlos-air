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
        # No test of the existing monitor may touch the real NetworkManager.
        self.signal_patch = patch.object(network, "wifi_signal", return_value=None)
        self.signal = self.signal_patch.start()
        self.addCleanup(self.signal_patch.stop)

    def tearDown(self):
        self.temp.cleanup()

    def counters(self, rx, tx, interface="wlp6s0"):
        stats = self.sys_root / interface / "statistics"
        stats.mkdir(parents=True, exist_ok=True)
        (stats / "rx_bytes").write_text(str(rx))
        (stats / "tx_bytes").write_text(str(tx))

    def sample_at(self, monitor, now, route=None):
        with patch.object(network.time, "monotonic", return_value=now), \
                patch.object(network, "discover_route", return_value=self.route if route is None else route), \
                patch.object(network, "ping_once", return_value=(2., "ok", "")):
            return monitor._sample()

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

    def test_rates_need_baseline_and_use_elapsed_time(self):
        monitor = network.NetworkMonitor(sys_root=self.sys_root)
        self.counters(10000, 5000)
        first = self.sample_at(monitor, 100)
        self.assertIsNone(first["rx_bytes_sec"])
        self.assertIsNone(first["tx_bytes_sec"])
        self.counters(15000, 7500)
        second = self.sample_at(monitor, 105)
        self.assertEqual(second["rx_bytes_sec"], 1000)
        self.assertEqual(second["tx_bytes_sec"], 500)
        # No traffic is a valid zero, rather than an unavailable measurement.
        third = self.sample_at(monitor, 110)
        self.assertEqual(third["rx_bytes_sec"], 0)
        self.assertEqual(third["tx_bytes_sec"], 0)

    def test_counter_reset_or_missing_value_restarts_baseline(self):
        monitor = network.NetworkMonitor(sys_root=self.sys_root)
        self.counters(10000, 5000)
        self.sample_at(monitor, 100)
        self.counters(100, 10)
        reset = self.sample_at(monitor, 105)
        self.assertIsNone(reset["rx_bytes_sec"])
        self.assertIsNone(reset["tx_bytes_sec"])
        self.counters(600, 510)
        self.assertEqual(self.sample_at(monitor, 110)["rx_bytes_sec"], 100)
        (self.sys_root / "wlp6s0/statistics/tx_bytes").unlink()
        missing = self.sample_at(monitor, 115)
        self.assertIsNone(missing["rx_bytes_sec"])
        self.assertIsNone(missing["tx_bytes_sec"])
        self.counters(1000000, 1000000)
        reappeared = self.sample_at(monitor, 120)
        self.assertIsNone(reappeared["rx_bytes_sec"])
        self.counters(1000500, 1001000)
        recovered = self.sample_at(monitor, 125)
        self.assertEqual(recovered["rx_bytes_sec"], 100)
        self.assertEqual(recovered["tx_bytes_sec"], 200)

    def test_interface_change_and_disconnect_cannot_create_spikes(self):
        monitor = network.NetworkMonitor(sys_root=self.sys_root)
        self.counters(10000, 5000)
        self.sample_at(monitor, 100)
        self.counters(10000000, 20000000, interface="eth0")
        (self.sys_root / "eth0/operstate").write_text("up")
        route = {**self.route, "interface": "eth0", "connection": "ethernet"}
        monitor._route_at = 0
        changed = self.sample_at(monitor, 105, route)
        self.assertIsNone(changed["rx_bytes_sec"])
        self.assertIsNone(changed["tx_bytes_sec"])
        self.assertIsNone(changed["signal_percent"])
        (self.sys_root / "eth0/operstate").write_text("down")
        disconnected = self.sample_at(monitor, 110, route)
        self.assertEqual(disconnected["status"], "disconnected")
        self.assertIsNone(disconnected["rx_bytes_sec"])
        (self.sys_root / "eth0/operstate").write_text("up")
        self.counters(20000000, 30000000, interface="eth0")
        reconnected = self.sample_at(monitor, 115, route)
        self.assertIsNone(reconnected["rx_bytes_sec"])

    def test_absent_route_and_invalid_clock_discard_rate_baseline(self):
        monitor = network.NetworkMonitor(sys_root=self.sys_root)
        self.counters(100, 200)
        self.sample_at(monitor, 100)
        self.counters(200, 300)
        self.assertIsNone(self.sample_at(monitor, 100)["rx_bytes_sec"])
        monitor._route_at = 0
        with patch.object(network, "discover_route", return_value=None):
            self.assertEqual(monitor._sample()["status"], "no_route")
        self.assertIsNone(monitor._traffic_previous)
        monitor._route_at = 0
        self.counters(10000000, 10000000)
        self.assertIsNone(self.sample_at(monitor, 110)["rx_bytes_sec"])

    def test_signal_cached_without_more_ping_packets(self):
        monitor = network.NetworkMonitor(sys_root=self.sys_root)
        self.signal.side_effect = [74, None, 81]
        with patch.object(network.time, "monotonic") as clock, \
                patch.object(network, "discover_route", return_value=self.route), \
                patch.object(network, "ping_once", return_value=(2., "ok", "")) as ping:
            for timestamp, expected in [(100, 74), (105, 74), (110, 74),
                                        (115, None), (120, None), (130, 81)]:
                clock.return_value = timestamp
                self.assertEqual(monitor._sample()["signal_percent"], expected)
            self.assertEqual(ping.call_count, 12)  # Two probes per existing sample.
        self.assertEqual(self.signal.call_count, 3)

    def test_traffic_counter_validation_and_interface_safety(self):
        for interface in ("..", "../wlp6s0", "/sys/class/net/eth0", "bad\nname", None):
            with self.subTest(interface=interface), patch.object(network.Path, "read_text") as read:
                self.assertIsNone(network.traffic_counters(interface, self.sys_root))
                read.assert_not_called()
        for value in (-1, "bad", "NaN", "3.5", 2**64):
            self.counters(value, 500)
            self.assertIsNone(network.traffic_counters("wlp6s0", self.sys_root))


class WifiSignalTests(unittest.TestCase):
    """Only mocked nmcli calls: no scans or real NetworkManager access."""

    def test_only_active_signal_is_read_and_rescan_explicitly_disabled(self):
        completed = subprocess.CompletedProcess([], 0, ":98\n*:73\n:45\n", "")
        with patch.object(network.shutil, "which", return_value="/usr/bin/nmcli"), \
                patch.object(network.subprocess, "run", return_value=completed) as run:
            self.assertEqual(network.wifi_signal("wlp6s0"), 73)
        args, kwargs = run.call_args
        command = args[0]
        self.assertEqual(command[command.index("--rescan") + 1], "no")
        self.assertEqual(command[command.index("-f") + 1], "IN-USE,SIGNAL")
        self.assertEqual(command[-2:], ["ifname", "wlp6s0"])
        self.assertNotIn("shell", kwargs)
        self.assertEqual(kwargs["env"]["LC_ALL"], "C")
        self.assertLessEqual(kwargs["timeout"], 1)

    def test_unavailable_ambiguous_or_invalid_signals_are_none(self):
        with patch.object(network.shutil, "which", return_value="/usr/bin/nmcli"), \
                patch.object(network.subprocess, "run") as run:
            for output in ("", ":90\n", "*:101\n", "*:-1\n", "*:NaN\n",
                           "*:75\n*:65\n", "*:75%\n"):
                run.return_value = subprocess.CompletedProcess([], 0, output, "")
                self.assertIsNone(network.wifi_signal("wlp6s0"))
            for output, expected in (("*:0\n", 0), ("*:100\n", 100)):
                run.return_value = subprocess.CompletedProcess([], 0, output, "")
                self.assertEqual(network.wifi_signal("wlp6s0"), expected)
            run.return_value = subprocess.CompletedProcess([], 1, "*:75\n", "NM unavailable")
            self.assertIsNone(network.wifi_signal("wlp6s0"))
            run.side_effect = subprocess.TimeoutExpired("nmcli", 1)
            self.assertIsNone(network.wifi_signal("wlp6s0"))
            run.side_effect = OSError("missing executable")
            self.assertIsNone(network.wifi_signal("wlp6s0"))

    def test_absent_nmcli_and_unsafe_interfaces_do_not_spawn(self):
        with patch.object(network.shutil, "which", return_value=None), \
                patch.object(network.subprocess, "run") as run:
            self.assertIsNone(network.wifi_signal("wlp6s0"))
            for interface in ("..", "../wlp6s0", "bad\nname", None):
                self.assertIsNone(network.wifi_signal(interface))
            run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
