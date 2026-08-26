#!/usr/bin/env python3
"""Machine Pulse — 本机系统状态监控服务。"""

from __future__ import annotations

import json
import os
import platform
import socket
import sys
import threading
import time
import webbrowser
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import psutil


def app_root() -> Path:
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS)
    return Path(__file__).resolve().parent


ROOT = app_root()
BOOT_TIME = psutil.boot_time()

_lock = threading.Lock()
_snapshot: dict | None = None
_snapshot_json = b"{}"
_prev_net = psutil.net_io_counters()
_prev_net_t = time.time()
_prev_disk = psutil.disk_io_counters()
_prev_disk_t = time.time()
_stop = threading.Event()


def _sample_loop() -> None:
    global _snapshot, _snapshot_json
    psutil.cpu_percent(interval=None, percpu=True)
    for proc in psutil.process_iter(["cpu_percent"]):
        try:
            proc.cpu_percent(interval=None)
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
    while not _stop.is_set():
        cores = psutil.cpu_percent(interval=1.0, percpu=True)
        try:
            data = collect(cores)
            payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
            with _lock:
                _snapshot = data
                _snapshot_json = payload
        except Exception:
            continue


def bytes_human(n: float | None) -> str:
    if n is None:
        return "—"
    n = float(n)
    units = ("B", "KB", "MB", "GB", "TB", "PB")
    i = 0
    while n >= 1024 and i < len(units) - 1:
        n /= 1024.0
        i += 1
    if i == 0:
        return f"{int(n)} {units[i]}"
    return f"{n:.1f} {units[i]}"


def rate_human(n: float) -> str:
    return f"{bytes_human(n)}/s"


def cpu_brand() -> str:
    if platform.system() == "Windows":
        try:
            import winreg

            key = winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"HARDWARE\DESCRIPTION\System\CentralProcessor\0",
            )
            name, _ = winreg.QueryValueEx(key, "ProcessorNameString")
            winreg.CloseKey(key)
            return str(name).strip()
        except OSError:
            pass
    return platform.processor() or "Unknown CPU"


def uptime_human(seconds: float) -> str:
    seconds = int(max(0, seconds))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    parts = []
    if days:
        parts.append(f"{days}天")
    parts.append(f"{hours:02d}:{minutes:02d}:{secs:02d}")
    return " ".join(parts)


def collect(cpu_cores: list[float] | None = None) -> dict:
    global _prev_net, _prev_net_t, _prev_disk, _prev_disk_t

    now = time.time()
    if cpu_cores is None:
        cpu_cores = psutil.cpu_percent(interval=None, percpu=True)
    cpu_total = round(sum(cpu_cores) / len(cpu_cores), 1) if cpu_cores else 0.0
    freq = psutil.cpu_freq()
    vm = psutil.virtual_memory()
    swap = psutil.swap_memory()

    disks = []
    total_disk = used_disk = 0
    for part in psutil.disk_partitions(all=False):
        if os.name == "nt" and ("cdrom" in (part.opts or "").lower() or not part.fstype):
            continue
        try:
            usage = psutil.disk_usage(part.mountpoint)
        except (PermissionError, OSError):
            continue
        total_disk += usage.total
        used_disk += usage.used
        disks.append(
            {
                "device": part.device,
                "mount": part.mountpoint,
                "fstype": part.fstype,
                "total": usage.total,
                "used": usage.used,
                "free": usage.free,
                "percent": usage.percent,
                "total_h": bytes_human(usage.total),
                "used_h": bytes_human(usage.used),
                "free_h": bytes_human(usage.free),
            }
        )

    net = psutil.net_io_counters()
    dt_net = max(now - _prev_net_t, 0.001)
    sent_rate = max(0.0, (net.bytes_sent - _prev_net.bytes_sent) / dt_net)
    recv_rate = max(0.0, (net.bytes_recv - _prev_net.bytes_recv) / dt_net)
    _prev_net, _prev_net_t = net, now

    disk_io = psutil.disk_io_counters()
    read_rate = write_rate = 0.0
    if disk_io and _prev_disk:
        dt_disk = max(now - _prev_disk_t, 0.001)
        read_rate = max(0.0, (disk_io.read_bytes - _prev_disk.read_bytes) / dt_disk)
        write_rate = max(0.0, (disk_io.write_bytes - _prev_disk.write_bytes) / dt_disk)
        _prev_disk, _prev_disk_t = disk_io, now
    elif disk_io:
        _prev_disk, _prev_disk_t = disk_io, now

    ifaces = []
    stats_map = psutil.net_if_stats()
    for name, addrs in psutil.net_if_addrs().items():
        st = stats_map.get(name)
        ipv4 = next((a.address for a in addrs if a.family == socket.AF_INET), None)
        ipv6 = next(
            (
                a.address
                for a in addrs
                if getattr(socket, "AF_INET6", None) and a.family == socket.AF_INET6
            ),
            None,
        )
        mac = next(
            (
                a.address
                for a in addrs
                if getattr(psutil, "AF_LINK", None) and a.family == psutil.AF_LINK
            ),
            None,
        )
        if not ipv4 and not ipv6:
            continue
        ifaces.append(
            {
                "name": name,
                "ipv4": ipv4,
                "ipv6": ipv6,
                "mac": mac,
                "isup": bool(st.isup) if st else None,
                "speed": st.speed if st else None,
            }
        )
    ifaces.sort(key=lambda n: (not n["isup"], n["name"].startswith("Loopback"), n["name"]))

    processes = []
    for proc in psutil.process_iter(["pid", "name", "cpu_percent", "memory_percent", "memory_info"]):
        try:
            info = proc.info
            mem = info.get("memory_info")
            processes.append(
                {
                    "pid": info.get("pid"),
                    "name": info.get("name") or "—",
                    "cpu": info.get("cpu_percent") or 0.0,
                    "mem": info.get("memory_percent") or 0.0,
                    "rss": mem.rss if mem else 0,
                    "rss_h": bytes_human(mem.rss) if mem else "—",
                }
            )
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
    processes.sort(key=lambda p: p["rss"], reverse=True)
    processes = processes[:10]

    battery = None
    try:
        bat = psutil.sensors_battery()
        if bat:
            battery = {
                "percent": bat.percent,
                "plugged": bat.power_plugged,
                "secs_left": bat.secsleft
                if bat.secsleft not in (psutil.POWER_TIME_UNLIMITED, psutil.POWER_TIME_UNKNOWN)
                else None,
            }
    except Exception:
        battery = None

    uname = platform.uname()
    disk_percent = round(used_disk / total_disk * 100, 1) if total_disk else 0.0

    return {
        "ts": int(now * 1000),
        "host": {
            "hostname": socket.gethostname(),
            "os": f"{uname.system} {uname.release}",
            "version": uname.version,
            "arch": uname.machine,
            "cpu_brand": cpu_brand(),
            "cpu_physical": psutil.cpu_count(logical=False) or 0,
            "cpu_logical": psutil.cpu_count(logical=True) or 0,
            "boot_time": int(BOOT_TIME * 1000),
            "uptime": uptime_human(now - BOOT_TIME),
            "uptime_sec": int(now - BOOT_TIME),
        },
        "cpu": {
            "percent": cpu_total,
            "per_core": cpu_cores,
            "freq_current": freq.current if freq else None,
            "freq_max": freq.max if freq else None,
            "freq_min": freq.min if freq else None,
        },
        "memory": {
            "total": vm.total,
            "available": vm.available,
            "used": vm.used,
            "free": vm.free,
            "percent": vm.percent,
            "total_h": bytes_human(vm.total),
            "used_h": bytes_human(vm.used),
            "available_h": bytes_human(vm.available),
            "swap_total": swap.total,
            "swap_used": swap.used,
            "swap_percent": swap.percent,
            "swap_total_h": bytes_human(swap.total),
            "swap_used_h": bytes_human(swap.used),
        },
        "disk": {
            "percent": disk_percent,
            "total": total_disk,
            "used": used_disk,
            "free": total_disk - used_disk,
            "total_h": bytes_human(total_disk),
            "used_h": bytes_human(used_disk),
            "free_h": bytes_human(total_disk - used_disk),
            "volumes": disks,
            "io": {
                "read_rate": read_rate,
                "write_rate": write_rate,
                "read_rate_h": rate_human(read_rate),
                "write_rate_h": rate_human(write_rate),
                "read_bytes": disk_io.read_bytes if disk_io else 0,
                "write_bytes": disk_io.write_bytes if disk_io else 0,
            },
        },
        "network": {
            "sent": net.bytes_sent,
            "recv": net.bytes_recv,
            "sent_h": bytes_human(net.bytes_sent),
            "recv_h": bytes_human(net.bytes_recv),
            "sent_rate": sent_rate,
            "recv_rate": recv_rate,
            "sent_rate_h": rate_human(sent_rate),
            "recv_rate_h": rate_human(recv_rate),
            "packets_sent": net.packets_sent,
            "packets_recv": net.packets_recv,
            "errin": net.errin,
            "errout": net.errout,
            "dropin": net.dropin,
            "dropout": net.dropout,
            "interfaces": ifaces,
        },
        "processes": {
            "count": len(psutil.pids()),
            "top": processes,
        },
        "battery": battery,
    }


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def log_message(self, fmt: str, *args) -> None:
        return

    def _send_bytes(self, payload: bytes, content_type: str, status: int = 200) -> None:
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError, OSError):
            return

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/stats":
            with _lock:
                payload = _snapshot_json
            self._send_bytes(payload, "application/json; charset=utf-8")
            return
        if path == "/favicon.ico":
            ico = ROOT / "icon.ico"
            if ico.exists():
                self._send_bytes(ico.read_bytes(), "image/x-icon")
            else:
                self.send_response(204)
                try:
                    self.end_headers()
                except OSError:
                    pass
            return
        if path in ("/", "/index.html"):
            page = ROOT / "index.html"
            if not page.exists():
                self._send_bytes("看板页面缺失。".encode("utf-8"), "text/plain; charset=utf-8", 404)
                return
            self._send_bytes(page.read_bytes(), "text/html; charset=utf-8")
            return
        super().do_GET()


def start_server() -> tuple[ThreadingHTTPServer, str]:
    host = "127.0.0.1"
    httpd = None
    bound = None
    ThreadingHTTPServer.allow_reuse_address = True
    for port in range(8765, 8780):
        try:
            httpd = ThreadingHTTPServer((host, port), Handler)
            bound = port
            break
        except OSError:
            continue
    if httpd is None or bound is None:
        raise OSError("无法绑定 8765-8779 端口，请关闭占用后重试。")

    sampler = threading.Thread(target=_sample_loop, name="cpu-sampler", daemon=True)
    sampler.start()
    worker = threading.Thread(target=httpd.serve_forever, name="http-server", daemon=True)
    worker.start()
    return httpd, f"http://{host}:{bound}/"


def stop_server(httpd: ThreadingHTTPServer) -> None:
    _stop.set()
    try:
        httpd.shutdown()
    except Exception:
        pass
    try:
        httpd.server_close()
    except Exception:
        pass


def main() -> None:
    httpd, url = start_server()
    print("=" * 48)
    print("  电脑健康看板")
    print(f"  打开浏览器访问: {url}")
    print("  按 Ctrl+C 停止服务")
    print("=" * 48)
    try:
        webbrowser.open(url)
    except Exception:
        pass
    try:
        while not _stop.is_set():
            time.sleep(0.4)
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        stop_server(httpd)


if __name__ == "__main__":
    main()
