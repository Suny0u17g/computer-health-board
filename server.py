#!/usr/bin/env python3
"""Machine Pulse — 本机系统状态监控服务。"""

from __future__ import annotations

import json
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from ctypes import Structure, byref, c_double, c_ulong, c_void_p, c_wchar, windll
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
_gpu_reader = None


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


class _PdhFmtValue(Structure):
    _fields_ = [("CStatus", c_ulong), ("doubleValue", c_double)]


PDH_FMT_DOUBLE = 0x00000200
PDH_FMT_NOCAP100 = 0x00008000


class GpuReader:
    """NVIDIA 走 nvidia-smi；Intel/AMD 走 Windows GPU Engine 计数器。读不到就只显示名字。"""

    def __init__(self) -> None:
        self.name = "显卡"
        self.memory_total = None
        self._nvidia = shutil.which("nvidia-smi")
        self._query = c_void_p()
        self._counters: list[tuple[c_void_p, str]] = []
        self._ready = False
        self._load_identity()
        if not self._nvidia:
            self._init_pdh()

    def _load_identity(self) -> None:
        if platform.system() != "Windows":
            return
        try:
            out = subprocess.check_output(
                [
                    "powershell",
                    "-NoProfile",
                    "-Command",
                    "Get-CimInstance Win32_VideoController | "
                    "Select-Object -First 1 Name, AdapterRAM | ConvertTo-Json -Compress",
                ],
                timeout=4,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                stderr=subprocess.DEVNULL,
            )
            info = json.loads(out.decode("utf-8", "replace") or "{}")
            if isinstance(info, list):
                info = info[0] if info else {}
            self.name = str(info.get("Name") or "显卡").strip()
            ram = info.get("AdapterRAM")
            if isinstance(ram, int) and ram > 0:
                self.memory_total = ram
        except Exception:
            pass

    def _init_pdh(self) -> None:
        pdh = windll.pdh
        if pdh.PdhOpenQueryW(None, None, byref(self._query)) != 0:
            return
        size = c_ulong(0)
        path = r"\GPU Engine(*)\Utilization Percentage"
        pdh.PdhExpandWildCardPathW(None, path, None, byref(size), 0)
        if size.value <= 2:
            return
        buf = (c_wchar * size.value)()
        if pdh.PdhExpandWildCardPathW(None, path, buf, byref(size), 0) != 0:
            return
        raw = ctypes_wstring_from_buffer(buf)
        wanted = ("engtype_3d", "engtype_compute", "engtype_videodecode", "engtype_videoprocessing")
        for item in raw:
            low = item.lower()
            if not any(tag in low for tag in wanted):
                continue
            handle = c_void_p()
            if pdh.PdhAddEnglishCounterW(self._query, item, None, byref(handle)) == 0:
                self._counters.append((handle, item))
        if self._counters:
            pdh.PdhCollectQueryData(self._query)
            self._ready = True

    def read(self) -> dict:
        if self._nvidia:
            data = self._read_nvidia()
            if data:
                return data
        percent = self._read_pdh()
        mem_used = None
        mem_pct = None
        if self.memory_total and mem_used is not None:
            mem_pct = round(mem_used / self.memory_total * 100, 1)
        available = percent is not None
        return {
            "available": available,
            "name": self.name,
            "percent": round(percent, 1) if percent is not None else None,
            "memory_used": mem_used,
            "memory_total": self.memory_total,
            "memory_used_h": bytes_human(mem_used) if mem_used else None,
            "memory_total_h": bytes_human(self.memory_total) if self.memory_total else None,
            "memory_percent": mem_pct,
            "source": "pdh" if available else "name-only",
        }

    def _read_nvidia(self) -> dict | None:
        try:
            out = subprocess.check_output(
                [
                    self._nvidia,
                    "--query-gpu=name,utilization.gpu,memory.used,memory.total",
                    "--format=csv,noheader,nounits",
                ],
                timeout=2,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                stderr=subprocess.DEVNULL,
            )
            line = out.decode("utf-8", "replace").strip().splitlines()[0]
            name, util, used, total = [p.strip() for p in line.split(",")]
            used_b = float(used) * 1024 * 1024
            total_b = float(total) * 1024 * 1024
            pct = float(util)
            return {
                "available": True,
                "name": name,
                "percent": pct,
                "memory_used": used_b,
                "memory_total": total_b,
                "memory_used_h": bytes_human(used_b),
                "memory_total_h": bytes_human(total_b),
                "memory_percent": round(used_b / total_b * 100, 1) if total_b else None,
                "source": "nvidia-smi",
            }
        except Exception:
            return None

    def _read_pdh(self) -> float | None:
        if not self._ready:
            return None
        pdh = windll.pdh
        if pdh.PdhCollectQueryData(self._query) != 0:
            return None
        by_pid: dict[str, float] = {}
        for handle, path in self._counters:
            val = _PdhFmtValue()
            if pdh.PdhGetFormattedCounterValue(handle, PDH_FMT_DOUBLE | PDH_FMT_NOCAP100, None, byref(val)) != 0:
                continue
            if val.CStatus not in (0, 1):
                continue
            m = re.search(r"pid_(\d+)", path, re.I)
            pid = m.group(1) if m else path
            by_pid[pid] = max(by_pid.get(pid, 0.0), float(val.doubleValue))
        if not by_pid:
            return 0.0
        return min(100.0, sum(by_pid.values()))


def ctypes_wstring_from_buffer(buf) -> list[str]:
    text = "".join(buf)
    return [p for p in text.split("\x00") if p]


def gpu_reader() -> GpuReader:
    global _gpu_reader
    if _gpu_reader is None:
        _gpu_reader = GpuReader()
    return _gpu_reader


def collect_gpu() -> dict:
    try:
        return gpu_reader().read()
    except Exception:
        return {
            "available": False,
            "name": "显卡",
            "percent": None,
            "memory_used": None,
            "memory_total": None,
            "memory_used_h": None,
            "memory_total_h": None,
            "memory_percent": None,
            "source": "unavailable",
        }


def _level(value: float, warn_at: float, bad_at: float) -> str:
    if value >= bad_at:
        return "bad"
    if value >= warn_at:
        return "warn"
    return "ok"


def _worst(levels: list[str]) -> str:
    if "bad" in levels:
        return "bad"
    if "warn" in levels:
        return "warn"
    return "ok"


def diagnose(data: dict) -> dict:
    cpu = float((data.get("cpu") or {}).get("percent") or 0)
    mem = float((data.get("memory") or {}).get("percent") or 0)
    swap = float((data.get("memory") or {}).get("swap_percent") or 0)
    volumes = (data.get("disk") or {}).get("volumes") or []
    c_drive = next((v for v in volumes if str(v.get("mount", "")).upper().startswith("C:")), volumes[0] if volumes else None)
    full_vols = [v for v in volumes if float(v.get("percent") or 0) >= 90]
    tight_vols = [v for v in volumes if 80 <= float(v.get("percent") or 0) < 90]
    ifaces = (data.get("network") or {}).get("interfaces") or []
    live = [n for n in ifaces if n.get("isup") and "loopback" not in str(n.get("name", "")).lower()]
    recv = float((data.get("network") or {}).get("recv_rate") or 0)
    sent = float((data.get("network") or {}).get("sent_rate") or 0)
    busy_net = recv + sent > 2 * 1024 * 1024
    gpu = data.get("gpu") or {}
    gpu_pct = gpu.get("percent")
    bat = data.get("battery")

    cpu_lv = _level(cpu, 55, 85)
    mem_lv = "bad" if mem >= 90 or swap >= 40 else "warn" if mem >= 75 or swap >= 20 else "ok"
    disk_lv = "bad" if full_vols else "warn" if tight_vols or (c_drive and float(c_drive.get("percent") or 0) >= 80) else "ok"
    net_lv = "bad" if not live else "warn" if busy_net else "ok"
    gpu_lv = "ok"
    if gpu.get("available") and gpu_pct is not None:
        gpu_lv = _level(float(gpu_pct), 70, 90)
    bat_lv = "ok"
    if bat and not bat.get("plugged"):
        bp = float(bat.get("percent") or 0)
        bat_lv = "bad" if bp <= 15 else "warn" if bp <= 25 else "ok"

    findings: list[dict] = []
    alerts: list[dict] = []

    if cpu_lv == "ok":
        findings.append({"level": "ok", "title": "处理器", "text": f"现在只用了 {cpu:.0f}%，很轻松，可以正常办公、上网、看视频。"})
    elif cpu_lv == "warn":
        text = f"已经用到 {cpu:.0f}%，电脑会开始发热、风扇变响。先别开太多大软件。"
        findings.append({"level": "warn", "title": "处理器", "text": text})
        alerts.append({"id": "cpu_high", "level": "warn", "title": "处理器比较忙", "text": text})
    else:
        text = f"已经用到 {cpu:.0f}%，很容易卡顿。建议关掉暂时不用的软件。"
        findings.append({"level": "bad", "title": "处理器", "text": text})
        alerts.append({"id": "cpu_high", "level": "bad", "title": "处理器几乎满载", "text": text})

    used_h = (data.get("memory") or {}).get("used_h", "—")
    total_h = (data.get("memory") or {}).get("total_h", "—")
    if mem_lv == "ok":
        findings.append({"level": "ok", "title": "内存", "text": f"已用 {mem:.0f}%（{used_h} / {total_h}），还够用。"})
    elif mem_lv == "warn":
        text = f"已用 {mem:.0f}%，再开新软件可能变慢。可以关掉浏览器多余标签页。"
        findings.append({"level": "warn", "title": "内存", "text": text})
        alerts.append({"id": "mem_high", "level": "warn", "title": "内存已经偏紧", "text": text})
    else:
        text = f"内存几乎满了（{mem:.0f}%）。现在最容易卡，请关掉占内存大的程序。"
        findings.append({"level": "bad", "title": "内存", "text": text})
        alerts.append({"id": "mem_high", "level": "bad", "title": "内存快满了", "text": text})

    if c_drive:
        mount = c_drive.get("mount", "C:\\")
        if float(c_drive.get("percent") or 0) >= 90:
            text = f"{mount} 只剩 {c_drive.get('free_h')}，系统盘太满了。请清理文件或把资料挪到其他盘。"
            findings.append({"level": "bad", "title": "硬盘", "text": text})
            alerts.append({"id": "disk_full", "level": "bad", "title": "系统盘空间不够", "text": text})
        elif float(c_drive.get("percent") or 0) >= 80:
            text = f"{mount} 已经用了 {c_drive.get('percent')}%，建议清理一下，避免更新失败、开机变慢。"
            findings.append({"level": "warn", "title": "硬盘", "text": text})
            alerts.append({"id": "disk_full", "level": "warn", "title": "系统盘开始偏满", "text": text})
        else:
            findings.append({"level": "ok", "title": "硬盘", "text": f"{mount} 还剩 {c_drive.get('free_h')}，空间够用。"})

    if not live:
        text = "没有检测到正在工作的网卡，现在可能上不了网。"
        findings.append({"level": "bad", "title": "网络", "text": text})
        alerts.append({"id": "net_down", "level": "bad", "title": "网络可能没连上", "text": text})
    elif busy_net:
        text = f"正在大量传数据（下 {(data.get('network') or {}).get('recv_rate_h')} / 上 {(data.get('network') or {}).get('sent_rate_h')}），可能在下载、同步或更新。"
        findings.append({"level": "warn", "title": "网络", "text": text})
    else:
        wifi = next((n for n in live if re.search(r"wlan|wi-?fi|无线", str(n.get("name", "")), re.I)), None)
        if wifi:
            findings.append({"level": "ok", "title": "网络", "text": f"无线网已连接（{wifi.get('ipv4') or '已联网'}），网速正常。"})
        else:
            findings.append({"level": "ok", "title": "网络", "text": f"网络已连接（{live[0].get('ipv4') or live[0].get('name')}）。"})

    if gpu.get("available") and gpu_pct is not None:
        if gpu_lv == "ok":
            findings.append({"level": "ok", "title": "显卡", "text": f"{gpu.get('name')} 现在用了 {float(gpu_pct):.0f}%，看视频、办公都够用。"})
        elif gpu_lv == "warn":
            text = f"显卡已经用到 {float(gpu_pct):.0f}%。如果在看视频或开会，这是正常的；风扇可能会响一些。"
            findings.append({"level": "warn", "title": "显卡", "text": text})
            alerts.append({"id": "gpu_high", "level": "warn", "title": "显卡比较忙", "text": text})
        else:
            text = f"显卡几乎满载（{float(gpu_pct):.0f}%）。如果不是在玩游戏或剪视频，可能有程序在后台占显卡。"
            findings.append({"level": "bad", "title": "显卡", "text": text})
            alerts.append({"id": "gpu_high", "level": "bad", "title": "显卡几乎满载", "text": text})
    elif gpu.get("name"):
        findings.append({"level": "ok", "title": "显卡", "text": f"检测到 {gpu.get('name')}。这台电脑读不到实时占用时，会只显示名字。"})

    if bat and not bat.get("plugged") and float(bat.get("percent") or 0) <= 25:
        text = f"电池还剩 {float(bat.get('percent')):.0f}%，而且没插电。电量低时电脑会变慢，建议充电。"
        findings.append({"level": bat_lv, "title": "电池", "text": text})
        alerts.append({"id": "battery_low", "level": bat_lv, "title": "电池电量偏低", "text": text})

    overall = _worst([cpu_lv, mem_lv, disk_lv, net_lv, gpu_lv, bat_lv])
    title = "状态良好"
    summary = "没有明显问题，可以放心用。下面每项都有白话说明。"
    if overall == "warn":
        title = "需要留意"
        reasons = []
        if mem_lv == "warn":
            reasons.append("内存已经偏紧")
        if cpu_lv == "warn":
            reasons.append("处理器比较忙")
        if gpu_lv == "warn":
            reasons.append("显卡比较忙")
        if disk_lv == "warn":
            reasons.append("硬盘开始偏满")
        if net_lv == "warn":
            reasons.append("网络正在大量传数据")
        if bat_lv == "warn":
            reasons.append("电池电量偏低")
        summary = ( "，".join(reasons) or "有几项开始偏高") + "。优先看橙色那几条，现在还能用。"
    elif overall == "bad":
        title = "有问题"
        reasons = []
        if mem_lv == "bad":
            reasons.append("内存快满了，容易卡")
        if cpu_lv == "bad":
            reasons.append("处理器几乎满载")
        if gpu_lv == "bad":
            reasons.append("显卡几乎满载")
        if disk_lv == "bad":
            reasons.append("系统盘空间不够")
        if net_lv == "bad":
            reasons.append("网络可能没连上")
        if bat_lv == "bad":
            reasons.append("电池电量很低")
        summary = ("；".join(reasons) or "有项目需要马上处理") + "。先看红色那几条。"

    return {
        "overall": overall,
        "title": title,
        "summary": summary,
        "findings": findings[:6],
        "alerts": alerts,
        "cpuLv": cpu_lv,
        "memLv": mem_lv,
        "diskLv": disk_lv,
        "netLv": net_lv,
        "gpuLv": gpu_lv,
        "batLv": bat_lv,
    }


def current_snapshot() -> dict | None:
    with _lock:
        return _snapshot


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

    payload = {
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
        "gpu": collect_gpu(),
    }
    payload["health"] = diagnose(payload)
    return payload


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
