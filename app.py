#!/usr/bin/env python3
"""电脑健康看板 — Windows 托盘程序。"""

from __future__ import annotations

import ctypes
import sys
import threading
import urllib.request
import webbrowser
from pathlib import Path

import pystray
from PIL import Image, ImageDraw

import server

APP_NAME = "电脑健康看板"
MUTEX_NAME = "Global\\MachinePulseHealthBoard"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
ERROR_ALREADY_EXISTS = 183

_httpd = None
_url = "http://127.0.0.1:8765/"
_mutex = None


def frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def exe_path() -> str:
    if frozen():
        return sys.executable
    return str(Path(__file__).resolve())


def find_running_url() -> str | None:
    for port in range(8765, 8780):
        url = f"http://127.0.0.1:{port}/"
        try:
            urllib.request.urlopen(url + "api/stats", timeout=0.4)
            return url
        except Exception:
            continue
    return None


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


def on_quit(icon: pystray.Icon, _item=None) -> None:
    icon.visible = False
    icon.stop()
    if _httpd is not None:
        threading.Thread(target=server.stop_server, args=(_httpd,), daemon=True).start()


def build_menu(icon: pystray.Icon) -> pystray.Menu:
    def toggle_autostart(icon2: pystray.Icon, item: pystray.MenuItem) -> None:
        set_autostart(not autostart_enabled())
        icon2.menu = build_menu(icon2)

    items = [
        pystray.MenuItem("打开健康看板", lambda *_: open_board(), default=True),
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
    icon = pystray.Icon(
        APP_NAME,
        make_icon_image(),
        APP_NAME,
    )
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
