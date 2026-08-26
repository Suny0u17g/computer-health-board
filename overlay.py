#!/usr/bin/env python3
"""迷你悬浮窗：健康灯 / 内存 / 网速 / 显卡。"""

from __future__ import annotations

import threading
import tkinter as tk
from typing import Callable

import server

COLORS = {
    "bg": "#0c1220",
    "ink": "#e8eef8",
    "muted": "#8ea0b8",
    "ok": "#3ee0d4",
    "warn": "#f5a524",
    "bad": "#ff5d73",
    "line": "#1c2a44",
}


class MiniOverlay:
    def __init__(self, on_open: Callable[[], None]) -> None:
        self.on_open = on_open
        self._root: tk.Tk | None = None
        self._visible = True
        self._offset = (0, 0)
        self._press = (0, 0)
        self._moved = False
        self._dragging = False
        self._job = None
        self._labels: dict[str, tk.Label] = {}
        self._dot: tk.Canvas | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, name="mini-overlay", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        root = self._root
        if root is not None:
            try:
                root.after(0, root.destroy)
            except Exception:
                pass

    def toggle(self) -> None:
        root = self._root
        if root is None:
            return
        def _toggle() -> None:
            self._visible = not self._visible
            if self._visible:
                root.deiconify()
            else:
                root.withdraw()
        try:
            root.after(0, _toggle)
        except Exception:
            pass

    def visible(self) -> bool:
        return self._visible

    def _run(self) -> None:
        root = tk.Tk()
        self._root = root
        root.title("电脑健康看板")
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        root.configure(bg=COLORS["bg"])
        try:
            root.attributes("-alpha", 0.94)
        except Exception:
            pass

        sw = root.winfo_screenwidth()
        root.geometry(f"+{max(20, sw - 360)}+24")

        frame = tk.Frame(root, bg=COLORS["bg"], padx=10, pady=8, highlightthickness=1, highlightbackground=COLORS["line"])
        frame.pack(fill="both", expand=True)

        top = tk.Frame(frame, bg=COLORS["bg"])
        top.pack(fill="x")
        self._dot = tk.Canvas(top, width=12, height=12, bg=COLORS["bg"], highlightthickness=0)
        self._dot.pack(side="left", padx=(0, 8))
        self._dot.create_oval(1, 1, 11, 11, fill=COLORS["ok"], outline=COLORS["ok"], tags="lamp")
        title = tk.Label(top, text="电脑状态", fg=COLORS["ink"], bg=COLORS["bg"], font=("Microsoft YaHei UI", 9, "bold"))
        title.pack(side="left")
        hint = tk.Label(top, text="单击打开看板 · 拖动可移动", fg=COLORS["muted"], bg=COLORS["bg"], font=("Microsoft YaHei UI", 8))
        hint.pack(side="right")

        row = tk.Frame(frame, bg=COLORS["bg"])
        row.pack(fill="x", pady=(8, 0))
        for key, caption in (("mem", "内存"), ("net", "网速"), ("gpu", "显卡")):
            cell = tk.Frame(row, bg=COLORS["bg"])
            cell.pack(side="left", padx=(0, 14))
            cap = tk.Label(cell, text=caption, fg=COLORS["muted"], bg=COLORS["bg"], font=("Microsoft YaHei UI", 8), takefocus=0)
            cap.pack(anchor="w")
            val = tk.Label(cell, text="—", fg=COLORS["ink"], bg=COLORS["bg"], font=("Consolas", 11), takefocus=0)
            val.pack(anchor="w")
            self._labels[key] = val

        self._bind_drag(root)
        self._refresh()
        root.mainloop()
        self._root = None

    def _bind_drag(self, widget: tk.Misc) -> None:
        widget.bind("<ButtonPress-1>", self._start_drag)
        widget.bind("<B1-Motion>", self._on_drag)
        widget.bind("<ButtonRelease-1>", self._end_drag)
        for child in widget.winfo_children():
            self._bind_drag(child)

    def _start_drag(self, event) -> None:
        root = self._root
        if root is None:
            return
        self._offset = (event.x_root - root.winfo_rootx(), event.y_root - root.winfo_rooty())
        self._press = (event.x_root, event.y_root)
        self._moved = False
        self._dragging = True
        try:
            root.grab_set_global()
        except Exception:
            try:
                root.grab_set()
            except Exception:
                pass
        return "break"

    def _on_drag(self, event) -> None:
        root = self._root
        if root is None or not self._dragging:
            return
        if abs(event.x_root - self._press[0]) + abs(event.y_root - self._press[1]) > 3:
            self._moved = True
        x = event.x_root - self._offset[0]
        y = event.y_root - self._offset[1]
        root.geometry(f"+{x}+{y}")
        return "break"

    def _end_drag(self, _event) -> None:
        root = self._root
        self._dragging = False
        if root is not None:
            try:
                root.grab_release()
            except Exception:
                pass
        if not self._moved:
            self.on_open()
        return "break"

    def _refresh(self) -> None:
        root = self._root
        if root is None:
            return
        data = server.current_snapshot() or {}
        health = data.get("health") or {}
        overall = health.get("overall") or "ok"
        color = COLORS.get(overall, COLORS["ok"])
        if self._dot is not None:
            self._dot.itemconfig("lamp", fill=color, outline=color)

        mem = (data.get("memory") or {}).get("percent")
        self._labels["mem"].configure(text=f"{mem:.0f}%" if isinstance(mem, (int, float)) else "—")

        net = data.get("network") or {}
        down = net.get("recv_rate_h") or "—"
        up = net.get("sent_rate_h") or "—"
        self._labels["net"].configure(text=f"↓{down}  ↑{up}")

        gpu = data.get("gpu") or {}
        if gpu.get("available") and gpu.get("percent") is not None:
            self._labels["gpu"].configure(text=f"{float(gpu['percent']):.0f}%")
        else:
            self._labels["gpu"].configure(text="—")

        self._job = root.after(2000, self._refresh)
