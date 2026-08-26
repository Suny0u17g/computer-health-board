#!/usr/bin/env python3
"""电脑健康看板 — Windows 托盘程序。"""

from __future__ import annotations

import ctypes
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

import pystray
from PIL import Image, ImageDraw

import overlay
import server

APP_NAME = "电脑健康看板"
MUTEX_NAME = "Global\\MachinePulseHealthBoard"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
ERROR_ALREADY_EXISTS = 183
ALERT_COOLDOWN_SEC = 30 * 60
ALERT_HOLD_SEC = 20

_httpd = None
_url = "http://127.0.0.1:8765/"
_mutex = None
_icon: pystray.Icon | None = None
_overlay: overlay.MiniOverlay | None = None
_notify_enabled = True
_last_alert_at: dict[str, float] = {}
_alert_seen_since: dict[str, float] = {}
_watch_stop = threading.Event()


def frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def exe_path() -> str:
    if frozen():
        return sys.executable
    return str(Path(__file__).resolve())


def find_running_url() -> str | None:
    for port in range(8765, 8780):
        url = f"http://{host_port(port)}/"
        try:
            urllib.request.urlopen(url + "api/stats", timeout=0.4)
            return url
        except Exception:
            continue
    return None


def host_port(port: int) -> str:
    return f"127.0.0.1:{port}"


def acquire_mutex() -> bool:
    global _mutex
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.CreateMutexW(None, False, MUTEX_NAME)
    _mutex = handle
    return kernel32.GetLastError() != ERROR_ALREADY_EXISTS


def open_board(url: str | None = None) -> None:
    webbrowser.open(url or _url)


def autostart_enabled() -> bool:
    if not frozen():
        return False
    try:
        import winreg

        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_READ)
        try:
            value, _ = winreg.QueryValueEx(key, APP_NAME)
        finally:
            winreg.CloseKey(key)
        return Path(str(value).strip('"')) == Path(exe_path())
    except OSError:
        return False


def set_autostart(enabled: bool) -> None:
    if not frozen():
        return
    import winreg

    key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE)
    try:
        if enabled:
            winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, f'"{exe_path()}"')
        else:
            try:
                winreg.DeleteValue(key, APP_NAME)
            except FileNotFoundError:
                pass
    finally:
        winreg.CloseKey(key)


def make_icon_image() -> Image.Image:
    img = Image.new("RGBA", (64, 64), (7, 11, 20, 255))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle((4, 4, 60, 60), radius=10, outline=(62, 224, 212, 255), width=3)
    draw.line([(12, 36), (22, 36), (28, 16), (38, 50), (44, 30), (54, 30)], fill=(62, 224, 212, 255), width=4)
    return img


def notify(title: str, text: str) -> None:
    icon = _icon
    if icon is None:
        return
    try:
        icon.notify(text, title)
    except Exception:
        pass


def watch_alerts() -> None:
    while not _watch_stop.is_set():
        time.sleep(5)
        if not _notify_enabled:
            continue
        data = server.current_snapshot() or {}
        health = data.get("health") or {}
        alerts = health.get("alerts") or []
        now = time.time()
        active_ids = {a.get("id") for a in alerts if a.get("id")}
        for key in list(_alert_seen_since):
            if key not in active_ids:
                _alert_seen_since.pop(key, None)

        for alert in alerts:
            aid = str(alert.get("id") or "")
            if not aid:
                continue
            first = _alert_seen_since.get(aid)
            if first is None:
                _alert_seen_since[aid] = now
                continue
            if now - first < ALERT_HOLD_SEC:
                continue
            last = _last_alert_at.get(aid, 0)
            if now - last < ALERT_COOLDOWN_SEC:
                continue
            _last_alert_at[aid] = now
            notify(str(alert.get("title") or "电脑需要留意"), str(alert.get("text") or health.get("summary") or ""))


def on_quit(icon: pystray.Icon, _item=None) -> None:
    _watch_stop.set()
    if _overlay is not None:
        _overlay.stop()
    icon.visible = False
    icon.stop()
    if _httpd is not None:
        threading.Thread(target=server.stop_server, args=(_httpd,), daemon=True).start()


def rebuild_menu(icon: pystray.Icon) -> None:
    icon.menu = build_menu(icon)


def build_menu(icon: pystray.Icon) -> pystray.Menu:
    def toggle_autostart(icon2: pystray.Icon, _item=None) -> None:
        set_autostart(not autostart_enabled())
        rebuild_menu(icon2)

    def toggle_overlay(icon2: pystray.Icon, _item=None) -> None:
        if _overlay is not None:
            _overlay.toggle()
        rebuild_menu(icon2)

    def toggle_notify(icon2: pystray.Icon, _item=None) -> None:
        global _notify_enabled
        _notify_enabled = not _notify_enabled
        rebuild_menu(icon2)

    items = [
        pystray.MenuItem("打开健康看板", lambda *_: open_board(), default=True),
        pystray.MenuItem(
            "显示迷你悬浮窗",
            toggle_overlay,
            checked=lambda _: bool(_overlay and _overlay.visible()),
        ),
        pystray.MenuItem(
            "异常时弹出通知",
            toggle_notify,
            checked=lambda _: _notify_enabled,
        ),
        pystray.Menu.SEPARATOR,
    ]
    if frozen():
        items.append(
            pystray.MenuItem(
                "开机自动启动",
                toggle_autostart,
                checked=lambda _: autostart_enabled(),
            )
        )
        items.append(pystray.Menu.SEPARATOR)
    items.append(pystray.MenuItem("退出", on_quit))
    return pystray.Menu(*items)


def run_tray() -> None:
    global _icon, _overlay
    _overlay = overlay.MiniOverlay(lambda: open_board())
    _overlay.start()
    threading.Thread(target=watch_alerts, name="alert-watch", daemon=True).start()
    icon = pystray.Icon(APP_NAME, make_icon_image(), APP_NAME)
    _icon = icon
    icon.menu = build_menu(icon)
    icon.run()


def main() -> None:
    global _httpd, _url

    existing = find_running_url()
    if not acquire_mutex():
        open_board(existing or "http://127.0.0.1:8765/")
        return
    if existing:
        open_board(existing)
        return

    try:
        _httpd, _url = server.start_server()
    except OSError:
        leftover = find_running_url()
        open_board(leftover or "http://127.0.0.1:8765/")
        return

    open_board(_url)
    run_tray()
    if _httpd is not None:
        server.stop_server(_httpd)


if __name__ == "__main__":
    main()
