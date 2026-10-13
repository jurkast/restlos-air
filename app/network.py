"""Small, asynchronous ICMP monitor for the local link and internet.

No sockets listen, no network scan is performed, and no administrator rights
are requested. Callbacks execute in the worker thread; GTK users must forward
them through GLib.idle_add. A missing ICMP reply does not establish that a
connection is offline.
"""
from collections import deque
import ipaddress
import json
import logging
import math
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import threading
import time


LOG = logging.getLogger(__name__)
_INTERFACE = re.compile(r"^[A-Za-z0-9_:@.-]{1,64}$")
_HOST_LABEL = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")
_LATENCY = re.compile(r"time\s*([=<])\s*([0-9]+(?:\.[0-9]+)?)\s*ms")


def validate_target(target):
    """Allow an IP address or conventional hostname, never command options."""
    if not isinstance(target, str) or not target or len(target) > 253:
        raise ValueError("Ungültiges Ping-Ziel")
    try:
        ipaddress.ip_address(target)
        return target
    except ValueError:
        hostname = target[:-1] if target.endswith(".") else target
        if not all(_HOST_LABEL.fullmatch(label) for label in hostname.split(".")):
            raise ValueError("Ping-Ziel muss eine IP-Adresse oder ein Hostname sein")
        return target


def _valid_interface(interface):
    return (isinstance(interface, str) and interface not in (".", "..")
            and bool(_INTERFACE.fullmatch(interface)))


def _connection(interface, sys_root):
    if not interface:
        return "unknown"
    if (sys_root / interface / "wireless").is_dir():
        return "wlan"
    if interface.startswith(("tun", "tap", "wg")):
        return "vpn"
    return "ethernet"


def _proc_route(path):
    """IPv4 fallback when the ip utility is unavailable."""
    routes = []
    try:
        for line in path.read_text().splitlines()[1:]:
            fields = line.split()
            if len(fields) < 8 or fields[1] != "00000000":
                continue
            interface = fields[0]
            flags = int(fields[3], 16)
            if not _valid_interface(interface) or not flags & 1:
                continue
            gateway = socket.inet_ntoa(int(fields[2], 16).to_bytes(4, "little"))
            routes.append({"interface": interface,
                           "gateway": None if gateway == "0.0.0.0" else gateway,
                           "metric": int(fields[6])})
    except (OSError, ValueError, OverflowError):
        return None
    return min(routes, key=lambda item: item["metric"]) if routes else None


def discover_route(sys_root=Path("/sys/class/net"),
                   proc_route=Path("/proc/net/route"), ipv6=False):
    """Find the lowest-metric default route without DNS or privileged probes."""
    route = None
    ip = shutil.which("ip")
    if ip:
        command = [ip, "-j"] + (["-6"] if ipv6 else [])
        command += ["route", "show", "default"]
        try:
            response = subprocess.run(command, capture_output=True, text=True,
                                      timeout=2, check=False)
            candidates = []
            if response.returncode == 0:
                for item in json.loads(response.stdout):
                    interface = item.get("dev")
                    if not _valid_interface(interface) or item.get("type", "unicast") != "unicast":
                        continue
                    gateway = item.get("gateway")
                    if gateway:
                        try:
                            gateway = str(ipaddress.ip_address(gateway))
                        except ValueError:
                            continue
                    candidates.append({"interface": interface, "gateway": gateway,
                                       "metric": int(item.get("metric", 0))})
                route = min(candidates, key=lambda item: item["metric"]) if candidates else None
        except (OSError, subprocess.TimeoutExpired, ValueError, TypeError, AttributeError):
            route = None
    if route is None and not ipv6:
        route = _proc_route(proc_route)
    if route:
        route["connection"] = _connection(route["interface"], sys_root)
    return route


def traffic_counters(interface, sys_root=Path("/sys/class/net")):
    """Read byte counters for one validated interface without extra processes."""
    if not _valid_interface(interface):
        return None
    try:
        stats = Path(sys_root) / interface / "statistics"
        counters = tuple(int((stats / field).read_text().strip())
                         for field in ("rx_bytes", "tx_bytes"))
        if any(value < 0 or value > 2**64 - 1 for value in counters):
            return None
        return counters
    except (OSError, ValueError):
        return None


def wifi_signal(interface):
    """NetworkManager's cached signal percentage for the active AP, no scan.

    Only IN-USE and SIGNAL are requested, so SSIDs and BSSIDs never enter the
    monitor. Missing nmcli, an inactive interface or an ambiguous result yields
    None. Signal percentage is not connection speed or packet delivery quality.
    """
    if not _valid_interface(interface):
        return None
    executable = shutil.which("nmcli")
    if not executable:
        return None
    command = [executable, "-t", "-f", "IN-USE,SIGNAL", "device", "wifi", "list",
               "--rescan", "no", "ifname", interface]
    try:
        response = subprocess.run(command, capture_output=True, text=True,
                                  timeout=1, check=False,
                                  env={**os.environ, "LC_ALL": "C"})
        if response.returncode != 0:
            return None
        active = [line.partition(":")[2].strip() for line in response.stdout.splitlines()
                  if line.partition(":")[0].strip() == "*"]
        if len(active) != 1 or not re.fullmatch(r"[0-9]{1,3}", active[0]):
            return None
        signal = int(active[0])
        return signal if 0 <= signal <= 100 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def ping_once(target, executable=None):
    """One bounded probe. Returns (milliseconds or None, status, explanation)."""
    validate_target(target)
    executable = executable or shutil.which("ping")
    if not executable:
        return None, "unavailable", "Ping ist nicht installiert"
    command = [executable, "-n", "-c", "1", "-W", "1", target]
    try:
        response = subprocess.run(command, capture_output=True, text=True,
                                  timeout=1.8, check=False,
                                  env={**os.environ, "LC_ALL": "C"})
    except subprocess.TimeoutExpired:
        return None, "no_reply", "Keine ICMP-Antwort innerhalb von 1 Sekunde"
    except OSError:
        return None, "error", "Ping konnte nicht gestartet werden"
    match = _LATENCY.search(response.stdout)
    if response.returncode == 0 and match:
        return float(match.group(2)), "ok", ""
    error = response.stderr.lower()
    if "operation not permitted" in error or "permission denied" in error:
        return None, "unavailable", "Das System erlaubt diesem Benutzer keine ICMP-Abfragen"
    if "name or service not known" in error or "temporary failure in name resolution" in error:
        return None, "error", "Der Name des Ping-Ziels kann nicht aufgelöst werden"
    if response.returncode == 1 or "network is unreachable" in error:
        return None, "no_reply", "Keine ICMP-Antwort; das Ziel kann Ping blockieren"
    return None, "error", "Ping liefert keine verwertbare Messung"


class NetworkMonitor:
    """One sleeping worker, two packets per interval, twelve recent samples.

    Internet ICMP round-trip time is to ``target``; it is not the ping of a
    particular game server. Gateway ping measures the local WLAN/LAN link.
    ``loss_percent`` is unanswered internet ICMP probes in ``samples`` recent
    attempts, not an estimate of all traffic loss. Route discovery is cached
    for thirty seconds; interface state is checked cheaply on every sample.
    RX/TX rates average the bytes transferred on that interface between samples.
    The first sample and counter discontinuities have no rate. WLAN signal is
    NetworkManager's cached active-AP percentage, refreshed at most every fifteen
    seconds without requesting a scan. All metrics use this existing worker.
    """

    def __init__(self, callback=None, interval=5.0, target="1.1.1.1",
                 sys_root=Path("/sys/class/net")):
        self.callback = callback
        self.interval = max(3.0, float(interval))
        self.target = validate_target(target)
        self.sys_root = Path(sys_root)
        self._route = None
        self._route_at = 0.0
        self._results = deque(maxlen=12)
        self._latencies = deque(maxlen=12)
        self._traffic_previous = None
        self._signal_percent = None
        self._signal_interface = None
        self._signal_at = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._thread = None
        self._latest = self._empty("measuring", "Messung startet")

    def _empty(self, status, detail):
        return {"updated_at": time.time(), "gateway_ms": None, "internet_ms": None,
                "gateway_status": status, "internet_status": status,
                "status": status, "interface": None, "connection": "unknown",
                "gateway": None, "internet_target": self.target,
                "loss_percent": None, "samples": 0, "jitter_ms": None,
                "signal_percent": None, "rx_bytes_sec": None, "tx_bytes_sec": None,
                "detail": detail}

    def snapshot(self):
        with self._lock:
            return dict(self._latest)

    def start(self):
        if self._thread and self._thread.is_alive():
            return self
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="restlos-air-ping", daemon=True)
        self._thread.start()
        return self

    def stop(self, join_timeout=3.0):
        self._stop.set()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=join_timeout)

    def _reset_link_metrics(self):
        self._traffic_previous = None
        self._signal_percent = None
        self._signal_interface = None
        self._signal_at = None

    def _traffic_rates(self, interface, now):
        counters = traffic_counters(interface, self.sys_root)
        previous = self._traffic_previous
        if counters is None:
            self._traffic_previous = None
            return None, None
        self._traffic_previous = (interface, now, *counters)
        if previous is None or previous[0] != interface:
            return None, None
        elapsed = now - previous[1]
        differences = tuple(value - old for value, old in zip(counters, previous[2:]))
        if not math.isfinite(elapsed) or elapsed <= 0 or any(value < 0 for value in differences):
            return None, None
        return tuple(value / elapsed for value in differences)

    def _cached_signal(self, route, now):
        if route["connection"] != "wlan":
            self._signal_percent = None
            self._signal_interface = None
            self._signal_at = None
            return None
        interface = route["interface"]
        if (self._signal_interface != interface or self._signal_at is None
                or now - self._signal_at >= 15):
            self._signal_percent = wifi_signal(interface)
            self._signal_interface = interface
            self._signal_at = now
        return self._signal_percent

    def _sample(self):
        now = time.monotonic()
        if now - self._route_at >= 30 or self._route_at == 0:
            try:
                ipv6 = ipaddress.ip_address(self.target).version == 6
            except ValueError:
                ipv6 = False
            route = discover_route(self.sys_root, ipv6=ipv6)
            if route != self._route:
                self._results.clear()
                self._latencies.clear()
                self._reset_link_metrics()
            self._route = route
            self._route_at = now
        route = self._route
        if not route:
            self._reset_link_metrics()
            return self._empty("no_route", "Keine Standardroute für dieses Ping-Ziel")
        result = self._empty("measuring", "")
        result.update({"interface": route["interface"], "connection": route["connection"],
                       "gateway": route["gateway"]})
        try:
            state = (self.sys_root / route["interface"] / "operstate").read_text().strip()
        except OSError:
            state = "unknown"
        if state in ("down", "notpresent", "lowerlayerdown"):
            self._route_at = 0.0
            self._results.clear()
            self._latencies.clear()
            self._reset_link_metrics()
            result.update(status="disconnected", internet_status="disconnected",
                          gateway_status="disconnected", detail="Netzwerkverbindung ist getrennt")
            return result
        rx_bytes_sec, tx_bytes_sec = self._traffic_rates(route["interface"], now)
        result.update(signal_percent=self._cached_signal(route, now),
                      rx_bytes_sec=rx_bytes_sec, tx_bytes_sec=tx_bytes_sec)
        if self._stop.is_set():
            return result
        if route["gateway"]:
            gateway_ms, gateway_status, _ = ping_once(route["gateway"])
        else:
            gateway_ms, gateway_status = None, "unavailable"
        if self._stop.is_set():
            return result
        internet_ms, internet_status, detail = ping_once(self.target)
        if internet_status in ("ok", "no_reply"):
            self._results.append(internet_status == "ok")
            self._latencies.append(internet_ms)
        pairs = [abs(right - left) for left, right in zip(self._latencies, list(self._latencies)[1:])
                 if left is not None and right is not None]
        result.update({"updated_at": time.time(), "gateway_ms": gateway_ms,
                       "gateway_status": gateway_status, "internet_ms": internet_ms,
                       "internet_status": internet_status, "status": internet_status,
                       "loss_percent": (100 * (1 - sum(self._results) / len(self._results)))
                       if self._results else None, "samples": len(self._results),
                       "jitter_ms": sum(pairs) / len(pairs) if pairs else None,
                       "detail": detail})
        return result

    def _run(self):
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                result = self._sample()
            except Exception:
                LOG.exception("Network monitor could not sample")
                self._reset_link_metrics()
                result = self._empty("error", "Netzwerkmessung vorübergehend nicht verfügbar")
            if self._stop.is_set():
                break
            with self._lock:
                self._latest = result
            if self.callback:
                try:
                    self.callback(dict(result))
                except Exception:
                    LOG.exception("Network monitor callback failed")
            self._stop.wait(max(0.1, self.interval - (time.monotonic() - started)))
