#!/usr/bin/env python3
"""AI Tool — graphical panel for Google Flow / Antigravity over the MLM VPN.

Run with pythonw (no console window):  pythonw agtool_gui.py
"""

import contextlib
import importlib
import io
import os
import queue
import re
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog

import agtool as t

BG = "#15171C"
CARD = "#1E2026"
CARD2 = "#272B33"
BTN = "#2E333C"
FG = "#E8EAED"
MUTED = "#9AA0A6"
GREEN = "#34A853"
RED = "#EA4335"
AMBER = "#F2A93B"
BLUE = "#2F6FEB"

ANSI = re.compile(r"\x1b\[[0-9;]*m")
STATE_ICON = {"ok": ("✓", GREEN), "warn": ("!", AMBER), "bad": ("✗", RED)}


def strip_ansi(text):
    return ANSI.sub("", text)


class App:
    def __init__(self, root):
        self.root = root
        self.busy = False
        self.buttons = []
        self._btn_style = {}      # button -> (text, bg, fg) so the busy state can be undone
        self._clicked_btn = None  # the button the user just pressed (to show the spinner there)
        self._busy_btn = None
        self._busy_title = ""
        self._busy_tick = 0
        self._busy_anim = None
        self._last_status = None  # snapshot of the last dashboard read, for change detection
        self._progress_pct = 0
        self._progress_visible = False
        self._collecting = False
        self._connected = None      # last known tunnel state (None until the first read)
        self._last_drop_alert = 0.0
        self._started = time.time()
        self.queue = queue.Queue()  # worker threads hand UI callables to the main loop
        t.PROGRESS = self._on_progress  # agtool reports real step percentages (0-100)
        self._base_title = "AI Tool — Google & Antigravity"
        root.title(self._base_title)
        root.geometry("1020x960")
        root.minsize(820, 620)
        root.configure(bg=BG)
        self._auto_connect_done = False
        self._cfg_stamp = self._config_stamp()
        self._build()
        root.after(100, self._drain)
        root.after(300, self.refresh)
        if t.AUTO_CONNECT:
            root.after(1500, self.action_connect_auto)
        root.after(2000, self._watch_config)
        root.after(60000, self._auto_refresh)

    def _drain(self):
        """Run UI updates queued by worker threads (tkinter is main-thread only)."""
        while True:
            try:
                fn = self.queue.get_nowait()
            except queue.Empty:
                break
            try:
                fn()
            except Exception:
                pass
        self.root.after(100, self._drain)

    # ------------------------------------------------------------------ UI
    def _build(self):
        pad = 16

        head = tk.Frame(self.root, bg=BG)
        head.pack(fill="x", padx=pad, pady=(pad, 6))
        tk.Label(head, text="AI Tool", bg=BG, fg=FG, font=("Segoe UI", 19, "bold")).pack(anchor="e")
        tk.Label(head, text="کنترل و تست Google Flow و Antigravity روی VPN",
                 bg=BG, fg=MUTED, font=("Segoe UI", 10)).pack(anchor="e")

        info = tk.Frame(self.root, bg=CARD)
        info.pack(fill="x", padx=pad, pady=(0, 8))
        self.info_card = info

        # real progress bar for long steps (shown only while an action reports progress)
        self.progress_row = tk.Frame(self.root, bg=CARD)
        self.progress_label = tk.Label(self.progress_row, text="", bg=CARD, fg=MUTED,
                                       font=("Segoe UI", 8), anchor="e")
        self.progress_label.pack(side="right", padx=(0, 12), pady=4)
        self.progress_canvas = tk.Canvas(self.progress_row, height=10, bg=CARD2,
                                         highlightthickness=0, bd=0)
        self.progress_canvas.pack(side="left", fill="x", expand=True, padx=12, pady=4)
        self.progress_fill = self.progress_canvas.create_rectangle(0, 0, 0, 10, fill=BLUE, width=0)
        self.progress_canvas.bind("<Configure>", lambda _event: self._draw_progress(self._progress_pct))
        self.lbl_node = tk.Label(info, text="نود: —", bg=CARD, fg=FG, font=("Segoe UI", 10, "bold"))
        self.lbl_node.pack(side="right", padx=(4, 16), pady=10)
        self.flag_img = None
        self.lbl_flag = tk.Label(info, bg=CARD)
        self.lbl_flag.pack(side="right", padx=(0, 6), pady=10)
        self.lbl_ip = tk.Label(info, text="IP: —", bg=CARD, fg=FG, font=("Segoe UI", 10))
        self.lbl_ip.pack(side="right", padx=16)
        self.lbl_time = tk.Label(info, text="در حال بررسی …", bg=CARD, fg=MUTED, font=("Segoe UI", 9))
        self.lbl_time.pack(side="left", padx=14)
        self.refresh_btn = tk.Button(info, text="بروزرسانی وضعیت", command=self.action_refresh_status, bg=CARD2, fg=FG,
                                     activebackground=CARD2, activeforeground=FG, relief="flat",
                                     bd=0, font=("Segoe UI", 9), padx=10, pady=4, cursor="hand2")
        self.refresh_btn.pack(side="left", padx=6)
        self.buttons.append(self.refresh_btn)
        self.settings_btn = tk.Button(info, text="تنظیمات", command=self.action_settings, bg=CARD2, fg=FG,
                                      activebackground=CARD2, activeforeground=FG, relief="flat",
                                      bd=0, font=("Segoe UI", 9), padx=10, pady=4, cursor="hand2")
        self.settings_btn.pack(side="left", padx=(0, 6))
        self.buttons.append(self.settings_btn)
        self.check_cfg_btn = tk.Button(info, text="چک تنظیمات", command=self.action_check_config,
                                       bg=CARD2, fg=FG, activebackground=CARD2, activeforeground=FG,
                                       relief="flat", bd=0, font=("Segoe UI", 9), padx=10, pady=4,
                                       cursor="hand2")
        self.check_cfg_btn.pack(side="left", padx=(0, 6))
        self.buttons.append(self.check_cfg_btn)

        vpnrow = tk.Frame(self.root, bg=CARD)
        vpnrow.pack(fill="x", padx=pad, pady=(0, 8))
        tk.Label(vpnrow, text="فیلترشکن:", bg=CARD, fg=MUTED,
                 font=("Segoe UI", 9)).pack(side="right", padx=(10, 4))
        self.vpn_choice = tk.StringVar(value=self._current_vpn_name())
        picker = tk.OptionMenu(vpnrow, self.vpn_choice, *[p["name"] for p in t.all_presets()],
                               command=self._on_vpn_pick)
        self.picker = picker
        picker.configure(bg=CARD2, fg=FG, activebackground=CARD2, activeforeground=FG, relief="flat",
                         bd=0, font=("Segoe UI", 9), highlightthickness=0, width=24)
        picker["menu"].configure(bg=CARD2, fg=FG, activebackground=BLUE, font=("Segoe UI", 9))
        picker.pack(side="right", padx=4, pady=6)
        apply_vpn_btn = tk.Button(vpnrow, text="اعمال", command=self.action_apply_vpn, bg=BLUE, fg="white",
                                  activebackground=BLUE, activeforeground="white", relief="flat", bd=0,
                                  font=("Segoe UI", 9, "bold"), padx=12, pady=5, cursor="hand2")
        apply_vpn_btn.pack(side="right", padx=4)
        detect_btn = tk.Button(vpnrow, text="تشخیص", command=self.action_detect_vpn, bg=CARD2, fg=FG,
                               activebackground=CARD2, activeforeground=FG, relief="flat", bd=0,
                               font=("Segoe UI", 9), padx=10, pady=5, cursor="hand2")
        detect_btn.pack(side="right", padx=4)
        # custom ports: keep the preset but with the user's own port numbers
        self.http_port_var = tk.StringVar(value=str(t.url_port(t.PROXY) or ""))
        self.socks_port_var = tk.StringVar(value=str(t.url_port(t.SOCKS) or ""))
        entry_style = dict(width=6, bg=CARD2, fg=FG, insertbackground=FG, relief="flat", bd=0,
                           justify="center", font=("Segoe UI", 9), highlightthickness=0)
        tk.Label(vpnrow, text="HTTP", bg=CARD, fg=MUTED, font=("Segoe UI", 8)).pack(side="right", padx=(8, 2))
        http_entry = tk.Entry(vpnrow, textvariable=self.http_port_var, **entry_style)
        http_entry.pack(side="right", pady=6, ipady=3)
        tk.Label(vpnrow, text="SOCKS", bg=CARD, fg=MUTED, font=("Segoe UI", 8)).pack(side="right", padx=(8, 2))
        socks_entry = tk.Entry(vpnrow, textvariable=self.socks_port_var, **entry_style)
        socks_entry.pack(side="right", pady=6, ipady=3)
        self.buttons.extend([apply_vpn_btn, detect_btn])

        summ = tk.Frame(self.root, bg=CARD, cursor="hand2")
        summ.pack(fill="x", padx=pad, pady=(0, 8))
        self.countries_row = tk.Frame(summ, bg=CARD, cursor="hand2")
        self.countries_row.pack(fill="x", padx=12, pady=3)
        self._flag_imgs = []
        self._summary_text("در حال بررسی …")
        summ.bind("<Button-1>", lambda _event: self.action_history())

        # widgets below are packed at the end, bottom-up, so they never get clipped
        card = tk.Frame(self.root, bg=CARD)
        self.checks_box = tk.Frame(card, bg=CARD)

        grid = tk.Frame(self.root, bg=BG)
        for col in range(3):
            grid.columnconfigure(col, weight=1)
        # right-to-left: the first item of every row sits in the right column
        layout = [
            ("تست دستی (Preflight)", self.action_preflight, BLUE),
            ("اتصال خودکار", self.action_connect, BLUE),
            ("باز کردن Google Flow", self.action_flow, BTN),
            ("اجرای Antigravity", self.action_ide, BTN),
            ("تعمیر تونل (اول همان نود)", self.action_heal, BTN),
            ("تاریخچهٔ IP و تعویض نود", self.action_history, BTN),
            ("نظارت خودکار: روشن/خاموش", self.action_watchdog, BTN),
            ("استارت خودکار با ویندوز", self.action_autostart, BTN),
            ("گزارش وضعیت کامل", self.action_status, BTN),
        ]
        for index, (text, handler, bg) in enumerate(layout):
            row, col = divmod(index, 3)
            b = tk.Button(grid, text=text, command=handler, bg=bg, fg="white",
                          activebackground=bg, activeforeground="white", relief="flat", bd=0,
                          font=("Segoe UI", 10, "bold"), padx=10, pady=7, cursor="hand2")
            b.grid(row=row, column=2 - col, sticky="ew", padx=4, pady=3)
            self.buttons.append(b)
            if text == "اتصال خودکار":
                self.connect_btn = b

        tools = tk.Frame(self.root, bg=BG)
        for col in range(4):
            tools.columnconfigure(col, weight=1)
        tool_items = [
            ("عیب‌یاب", self.action_doctor),
            ("پاک‌سازی کش", self.action_clean),
            ("بستن پروسه‌ها", self.action_kill),
            ("تست نودها", self.action_nodes),
            ("بروزرسانی برنامه", self.action_update),
            ("اجرای VPN", self.action_launchvpn),
            ("VPN دلخواه…", self.action_custom_vpn),
            ("پروکسی ترمینال", self.action_envproxy),
            ("اعتبار/کوتا", self.action_quota),
            ("ذخیرهٔ پروفایل", self.action_profile_export),
            ("بازیابی پروفایل", self.action_profile_import),
            ("بستهٔ عیب‌یابی", self.action_bundle),
            ("قفل PIN", self.action_pin),
            ("شورتکات IDE", self.action_shortcut),
            ("تست نشت DNS/IPv6", self.action_leak),
        ]
        for index, (text, handler) in enumerate(tool_items):
            row, col = divmod(index, 4)
            button = tk.Button(tools, text=text, command=handler, bg=CARD2, fg=FG,
                               activebackground=BTN, activeforeground=FG, relief="flat", bd=0,
                               font=("Segoe UI", 9), padx=4, pady=5, cursor="hand2")
            button.grid(row=row, column=col, sticky="ew", padx=3, pady=2)
            self.buttons.append(button)

        logf = tk.Frame(self.root, bg=CARD)
        self.log = tk.Text(logf, height=3, bg="#0F1116", fg="#C7CDD6", relief="flat",
                           font=("Consolas", 9), wrap="word", padx=8, pady=6)
        self.log.pack(fill="both", expand=True, padx=6, pady=6)
        self.log.configure(state="disabled")

        self.status = tk.Label(self.root, text="آماده", bg=BG, fg=MUTED, font=("Segoe UI", 9))

        # pack bottom-up first: status (lowest), log, buttons; the card takes the rest
        self.status.pack(side="bottom", anchor="e", padx=pad, pady=(0, 8))
        logf.pack(side="bottom", fill="x", padx=pad, pady=(8, 0))
        tools.pack(side="bottom", fill="x", padx=pad, pady=(8, 0))
        grid.pack(side="bottom", fill="x", padx=pad)
        card.pack(fill="both", expand=True, padx=pad, pady=(0, 4))
        self.checks_box.pack(fill="both", expand=True, padx=10, pady=8)

        # every button remembers its look (for the busy state) and which one was pressed
        for button in self.buttons:
            try:
                self._btn_style[button] = (button.cget("text"), button.cget("bg"), button.cget("fg"))
                button.bind("<Button-1>",
                            lambda _event, widget=button: setattr(self, "_clicked_btn", widget),
                            add="+")
            except tk.TclError:
                continue

    # ------------------------------------------------------------- helpers
    def log_line(self, text):
        self.log.configure(state="normal")
        self.log.insert("end", text.rstrip() + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _on_progress(self, percent, text=""):
        """Called from a worker thread by agtool.report_progress."""
        self.queue.put(lambda: self._show_progress(percent, text))

    def _show_progress(self, percent, text=""):
        self._progress_pct = max(0, min(100, int(percent)))
        step = (text or "").strip()
        if step:  # the taskbar shows the window title, so keep the step there too
            self._set_title(f"{self._progress_pct}% · {step}")
        if not self._progress_visible:
            try:
                self.progress_row.pack(fill="x", padx=16, pady=(0, 8), after=self.info_card)
                self._progress_visible = True
            except tk.TclError:
                return
        label = f"{self._progress_pct}%" + (f" · {text}" if text else "")
        self.progress_label.configure(text=label)
        self.progress_canvas.configure(bg=CARD2)
        self.progress_canvas.itemconfigure(self.progress_fill, fill=GREEN if self._progress_pct >= 100 else BLUE)
        self._draw_progress(self._progress_pct)

    def _draw_progress(self, percent):
        width = self.progress_canvas.winfo_width() or 1
        self.progress_canvas.coords(self.progress_fill, 0, 0, int(width * percent / 100.0), 10)

    def _set_title(self, step=""):
        """Window title doubles as the taskbar label: show the running step there."""
        try:
            self.root.title(f"{step} — AI Tool" if step else self._base_title)
        except tk.TclError:
            pass

    def _hide_progress(self):
        self._set_title()
        if not self._progress_visible:
            return
        try:
            self.progress_row.pack_forget()
        except tk.TclError:
            pass
        self._progress_visible = False
        self._progress_pct = 0

    def _hide_progress_if_idle(self):
        if not self.busy:
            self._hide_progress()

    def _start_busy(self, title, btn=None):
        """Show on the pressed button itself that its action is running."""
        self._hide_progress()
        self.busy = True
        self._busy_title = (title or "کار").strip()
        self.status.configure(text=f"⏳ در حال اجرا: {self._busy_title} …")
        target = btn or getattr(self, "_clicked_btn", None)
        self._clicked_btn = None  # a stale click must not light up on the next programmatic run
        self._busy_btn = target if target in self._btn_style else None
        self._busy_short = self._busy_title
        if len(self._busy_short) > 15:  # keep the grid width, cut on a word boundary
            self._busy_short = self._busy_title[:15].rsplit(" ", 1)[0].strip() or self._busy_title[:15]
        self._set_title(f"⏳ {self._busy_short}")
        if self._busy_btn is not None:
            try:
                self._busy_btn.configure(text=self._busy_label(), bg=AMBER, fg="#15171C")
            except tk.TclError:
                self._busy_btn = None
        for button in self.buttons:
            if button is not self._busy_btn:
                try:
                    button.configure(state="disabled")
                except tk.TclError:
                    pass
        self._busy_tick = 0
        self._animate_busy()

    def _busy_label(self, dots=0):
        """“⏳ عنوان …” for the pressed button, with moving dots."""
        short = getattr(self, "_busy_short", self._busy_title)
        tail = " …" if short == self._busy_title else ""
        return f"⏳ {short}{tail}{'.' * dots}"

    def _animate_busy(self):
        """Move the dots so a long action never looks frozen."""
        if not self.busy:
            return
        if self._busy_btn is not None:
            self._busy_tick = (self._busy_tick + 1) % 4
            try:
                self._busy_btn.configure(text=self._busy_label(self._busy_tick))
            except tk.TclError:
                self._busy_btn = None
        try:
            self._busy_anim = self.root.after(400, self._animate_busy)
        except tk.TclError:
            self._busy_anim = None

    def _end_busy(self, label="آماده"):
        self.busy = False
        if self._progress_visible:
            if self._progress_pct >= 90:
                self._show_progress(100, "تمام شد")
            else:
                self._show_progress(self._progress_pct, "متوقف شد")
            try:
                self.root.after(1200, self._hide_progress_if_idle)
            except tk.TclError:
                pass
        if self._busy_anim is not None:
            try:
                self.root.after_cancel(self._busy_anim)
            except tk.TclError:
                pass
            self._busy_anim = None
        if not self._progress_visible:
            self._set_title()
        btn, self._busy_btn = self._busy_btn, None
        if btn is not None and btn in self._btn_style:
            text, bg, fg = self._btn_style[btn]
            try:
                btn.configure(text=text, bg=bg, fg=fg)
            except tk.TclError:
                pass
        for button in self.buttons:
            try:
                button.configure(state="normal")
            except tk.TclError:
                pass
        self.status.configure(text=label, fg=MUTED)

    def _show_flag(self, path, code):
        """Real flag image when it could be fetched, country code otherwise."""
        self.flag_img = None
        if path:
            try:
                self.flag_img = tk.PhotoImage(file=path)
                self.lbl_flag.configure(image=self.flag_img, text="")
                return
            except Exception:
                self.flag_img = None
        self.lbl_flag.configure(image="", text=(code or ""), fg=MUTED, font=("Segoe UI", 9, "bold"))

    def render(self, data):
        for child in self.checks_box.winfo_children():
            child.destroy()
        for c in data["checks"]:
            icon, color = STATE_ICON.get(c["state"], ("•", MUTED))
            row = tk.Frame(self.checks_box, bg=CARD)
            row.pack(fill="x")
            tk.Label(row, text=icon, bg=CARD, fg=color, font=("Segoe UI", 10, "bold"),
                     width=2, height=1).pack(side="right")
            tk.Label(row, text=c["name"], bg=CARD, fg=FG, font=("Segoe UI", 8),
                     width=20, anchor="e").pack(side="right")
            tk.Label(row, text=c["detail"], bg=CARD, fg=MUTED, font=("Segoe UI", 8),
                     anchor="e").pack(side="right", padx=(0, 8))
        geo = data.get("geo") or {}
        self.lbl_node.configure(text=f"نود: {data['node']}")
        if geo.get("name"):
            self.lbl_ip.configure(text=f"IP: {geo['name']} ({geo['cc']}) · {data['ip']}")
        elif geo.get("cc"):
            self.lbl_ip.configure(text=f"IP: {geo['cc']} · {data['ip']}")
        else:
            self.lbl_ip.configure(text=f"IP: {data['ip'] or '—'}")
        self._show_flag(data.get("flag_path"), geo.get("cc"))
        verdict = "همه‌چیز سالم" if data["ok"] else "مشکل دارد — لاگ را ببین"
        self.lbl_time.configure(text=f"{data['time']} · {verdict}")
        self._render_countries(data)
        if not self.busy:
            self.status.configure(text="آماده")

    # -------------------------------------------------------------- actions
    def refresh(self):
        """Silent dashboard update (startup and once a minute, so a drop is noticed fast)."""
        if self.busy or self._collecting:
            return
        self._collecting = True
        threading.Thread(target=self._collect_worker, args=(False,), daemon=True).start()

    def _collect_worker(self, verdict):
        try:
            data = t.collect_status()
            data["flag_path"] = t.flag_png((data.get("geo") or {}).get("cc", ""))
            for country in data.get("countries") or []:  # download flags off the UI thread
                country["flag"] = t.flag_png(country.get("cc", ""))
            self.queue.put(lambda: self._apply_status(data, verdict))
        except Exception as exc:
            message = f"خواندن وضعیت ممکن نشد: {exc}"
            self.queue.put(lambda: self._status_failed(message))

    def _status_failed(self, message):
        self._collecting = False
        self.log_line(message)
        if self.busy:
            self._end_busy("بروزرسانی ناموفق")

    def _apply_status(self, data, verdict=False):
        self._collecting = False
        previous = self._last_status or {}
        self.render(data)
        self._last_status = {
            "ip": data.get("ip"),
            "node": data.get("node"),
            "ok": data.get("ok"),
            "time": data.get("time"),
            "checks": {c["name"]: c["state"] for c in data.get("checks") or []},
        }
        if verdict:
            self._report_status_change(previous, data)
            self._end_busy("آماده")
        self._track_connection(data)

    def _connection_state(self, data):
        """(is_up, reason) — the tunnel check is the decisive one."""
        checks = data.get("checks") or []
        tunnel = next((c for c in checks if "تونل" in c.get("name", "")), None)
        if tunnel is not None and tunnel.get("state") == "bad":
            return False, f"{tunnel.get('name')}: {tunnel.get('detail')}"
        if data.get("ok"):
            return True, ""
        broken = [c for c in checks if c.get("state") == "bad"]
        if broken:
            return False, "، ".join(f"{c['name']}" for c in broken)
        return True, ""

    def _track_connection(self, data):
        """Alert on its own whenever the tunnel drops (and once when it comes back)."""
        up, reason = self._connection_state(data)
        previous = self._connected
        self._connected = up
        if self.busy:  # an action is running: it reports its own result
            return
        if not up:
            self.status.configure(text=f"✗ اتصال قطع است — {reason}", fg=RED)
        elif self.status.cget("fg") == RED:
            self.status.configure(text="آماده", fg=MUTED)
        quiet = time.time() - self._started < 20  # no alarm during the startup connect
        now = time.time()
        if not up and (previous is True or previous is None) and not quiet:
            self.log_line(f"  ✗ اتصال قطع شد — {reason}")
            t.notify("AI Tool — اتصال قطع شد", f"{reason}\nبرای وصل شدن دکمهٔ «اتصال خودکار» را بزن")
            self._last_drop_alert = now
        elif not up and now - self._last_drop_alert > 600 and not quiet:
            t.notify("AI Tool — هنوز قطع است", f"{reason}\n«اتصال خودکار» را بزن")
            self._last_drop_alert = now
        elif up and previous is False:
            self.log_line("  ✓ اتصال دوباره برقرار شد")
            t.notify("AI Tool — اتصال برگشت", "تونل دوباره وصل است")

    def _report_status_change(self, previous, data):
        """Tell the user whether the tunnel is still up and what changed since the last read."""
        checks = data.get("checks") or []
        bad_checks = [c for c in checks if c.get("state") == "bad"]
        warn_checks = [c for c in checks if c.get("state") == "warn"]
        tunnel = next((c for c in checks if "تونل" in c.get("name", "")), None)
        ip, node = data.get("ip"), data.get("node")
        geo = data.get("geo") or {}
        where = f" · {geo.get('name') or geo.get('cc')}" if (geo.get('name') or geo.get('cc')) else ""

        if tunnel is not None and tunnel.get("state") == "bad":
            self.log_line(f"  ✗ اتصال برقرار نیست — {tunnel.get('name')}: {tunnel.get('detail')}")
        elif data.get("ok"):
            self.log_line(f"  ✓ اتصال برقرار است — IP: {ip or '—'}{where}")
        else:
            self.log_line(f"  ! اتصال کامل نیست — IP: {ip or '—'}{where}")
        if bad_checks:
            self.log_line("  ✗ موارد مشکل‌دار: " + "، ".join(f"{c['name']} ({c.get('detail')})" for c in bad_checks))
        elif warn_checks:
            self.log_line("  ! هشدار: " + "، ".join(c["name"] for c in warn_checks))

        changed = False
        if previous.get("ip") and ip and previous["ip"] != ip:
            self.log_line(f"  ! IP عوض شد: {previous['ip']} → {ip}{where}")
            changed = True
        if previous.get("node") and node and previous["node"] != node:
            self.log_line(f"  ! نود عوض شد: {previous['node']} → {node}")
            changed = True
        old_checks = previous.get("checks") or {}
        if old_checks:
            word = {"ok": "درست شد", "warn": "هشدار", "bad": "خراب شد"}
            diff = []
            for check in checks:
                name = check.get("name")
                if old_checks.get(name) != check.get("state"):
                    diff.append((name, word.get(check.get("state"), check.get("state"))))
            for name, text in diff:
                self.log_line(f"  ! {name}: {text}")
            changed = changed or bool(diff)
        if previous and not changed:
            self.log_line("  ✓ تغییری نسبت به بررسی قبلی نیست")
        self.log_line(f"  زمان بررسی: {data.get('time')}")

    def _auto_refresh(self):
        self.refresh()
        self.root.after(60000, self._auto_refresh)

    def run_action(self, title, fn, btn=None):
        if self.busy:
            return
        self._start_busy(title, btn)
        self.log_line(f"──── {title} ────")

        def work():
            buf = io.StringIO()
            err = None
            try:
                with contextlib.redirect_stdout(buf):
                    fn()
            except SystemExit:
                pass
            except Exception as exc:  # keep the UI alive on failures
                err = f"خطا: {exc}"
            text = strip_ansi(buf.getvalue()).strip()
            self.queue.put(lambda: self._finish(title, text, err))

        threading.Thread(target=work, daemon=True).start()

    def _finish(self, title, text, err):
        if text:
            self.log_line(text)
        if err:
            self.log_line(err)
        self._end_busy("آماده")
        self.refresh()

    def action_refresh_status(self):
        """Read the dashboard again and report whether the tunnel is still up / what changed."""
        if self.busy:
            return
        self._start_busy("بروزرسانی وضعیت", self.refresh_btn)
        self.log_line("──── بروزرسانی وضعیت ────")
        threading.Thread(target=self._collect_worker, args=(True,), daemon=True).start()

    def action_preflight(self):
        self.run_action("تست دستی قبل از باز کردن", lambda: t.cmd_preflight())

    def action_connect(self):
        self.run_action("اتصال خودکار", lambda: t.cmd_connect(), btn=getattr(self, "connect_btn", None))

    def action_connect_auto(self, attempt=0):
        """Run the startup connection once, waiting for the first refresh to finish."""
        if self._auto_connect_done:
            return
        if self.busy:
            if attempt < 25:
                self.root.after(800, lambda: self.action_connect_auto(attempt + 1))
            return
        self._auto_connect_done = True
        self.action_connect()

    def action_flow(self):
        self.run_action("باز کردن Google Flow", lambda: t.cmd_flow())

    def action_ide(self):
        self.run_action("اجرای Antigravity", lambda: t.cmd_ide())

    def action_heal(self):
        self.run_action("تعمیر تونل", lambda: t.cmd_heal())

    def action_history(self):
        self.run_action("تاریخچهٔ IP و تعویض نود", lambda: t.cmd_history())

    def action_status(self):
        self.run_action("وضعیت کامل", lambda: t.cmd_status())

    def action_leak(self):
        self.run_action("تست نشت DNS و IPv6", lambda: t.cmd_leak())

    def _bind_summary_click(self, widget):
        widget.bind("<Button-1>", lambda _event: self.action_history())
        for child in widget.winfo_children():
            self._bind_summary_click(child)

    def _summary_text(self, text):
        """Plain-text summary (used before the first status arrives and as a fallback)."""
        for child in self.countries_row.winfo_children():
            child.destroy()
        label = tk.Label(self.countries_row, text=f"۲۴ ساعت گذشته: {text}", bg=CARD, fg=MUTED,
                         font=("Segoe UI", 9), anchor="e", cursor="hand2")
        label.pack(side="right")
        self._bind_summary_click(self.countries_row)

    def _render_countries(self, data):
        """Countries of the last 24h with their flag images on the summary bar."""
        for child in self.countries_row.winfo_children():
            child.destroy()
        self._flag_imgs = []
        countries = data.get("countries") or []
        if not countries:
            self._summary_text("هیچ تغییر IP یا کشوری ثبت نشده")
            return
        counters = (f"{data.get('ips24', 0)} تغییر IP   ·   "
                    f"{data.get('switches24', 0)} تعویض نود   ·   "
                    f"{len(countries)} کشور")
        tk.Label(self.countries_row, text=counters, bg=CARD, fg=MUTED, font=("Segoe UI", 9),
                 cursor="hand2").pack(side="left", padx=(0, 8))
        tk.Label(self.countries_row, text="۲۴ ساعت گذشته:", bg=CARD, fg=MUTED,
                 font=("Segoe UI", 9), cursor="hand2").pack(side="right", padx=(0, 6))
        for country in countries:
            box = tk.Frame(self.countries_row, bg=CARD, cursor="hand2")
            box.pack(side="right", padx=8)
            name = country.get("name") or country.get("cc")
            tk.Label(box, text=f"{name} ({country.get('cc')})", bg=CARD, fg=FG,
                     font=("Segoe UI", 9), cursor="hand2").pack(side="right", padx=(5, 0))
            path = country.get("flag") or ""
            drawn = False
            if path:
                try:
                    image = tk.PhotoImage(file=path)
                    self._flag_imgs.append(image)  # keep a reference or Tk drops it
                    tk.Label(box, image=image, bg=CARD, cursor="hand2").pack(side="right")
                    drawn = True
                except Exception:
                    drawn = False
            if not drawn:
                tk.Label(box, text=country.get("cc") or "", bg=CARD, fg=MUTED,
                         font=("Segoe UI", 8, "bold"), cursor="hand2").pack(side="right")
        self._bind_summary_click(self.countries_row)

    def _current_vpn_name(self):
        preset = t.preset_by_id(t.current_vpn_id())
        presets = t.all_presets()
        return preset["name"] if preset else (presets[0]["name"] if presets else "")

    def _refresh_presets(self):
        """Custom VPNs added at runtime must show up in the picker."""
        menu = self.picker["menu"]
        menu.delete(0, "end")
        for preset in t.all_presets():
            menu.add_command(label=preset["name"],
                             command=lambda name=preset["name"]: (self.vpn_choice.set(name),
                                                                   self._on_vpn_pick()))

    def _pin_gate(self):
        """Ask for the PIN before a settings-changing action (no PIN = allowed)."""
        if not t.pin_is_set():
            return True
        pin = simpledialog.askstring("قفل تنظیمات", "PIN را وارد کن:", show="●", parent=self.root)
        if pin is None:
            return False
        if not t.pin_ok(pin):
            self.log_line("PIN درست نیست — اجازهٔ تغییر تنظیمات داده نشد")
            return False
        return True

    def _on_vpn_pick(self, *_args):
        """Fill the custom-port boxes with the picked preset's default ports."""
        preset = next((p for p in t.all_presets() if p["name"] == self.vpn_choice.get()), None)
        if not preset:
            return
        http_port, socks_port = t.preset_ports(preset)
        self.http_port_var.set("" if http_port is None else str(http_port))
        self.socks_port_var.set("" if socks_port is None else str(socks_port))

    def action_detect_vpn(self):
        http_port = self.http_port_var.get().strip()
        socks_port = self.socks_port_var.get().strip()

        def task():
            found = [f"{p['name']} — {p.get('http')}" for p in t.all_presets()
                     if p.get("mode") != "direct" and t.preset_detected(p)]
            if found:
                print("فیلترشکن‌های در حال اجرا:")
                for item in found:
                    print(f"  - {item}")
            else:
                print("هیچ فیلترشکن شناخته‌شده‌ای روی پورت پیش‌فرضش پیدا نشد")
            live = [f"{name} {raw}" for name, raw in (("HTTP", http_port), ("SOCKS", socks_port))
                    if raw.isdigit() and t.port_open(int(raw))]
            if live:
                print("پورت‌هایی که همین حالا جواب می‌دهند: " + " · ".join(live))
            elif http_port or socks_port:
                print("پورت‌های واردشده جواب ندادند — فیلترشکن روشن است؟")
            print("اگر فیلترشکن شما TUN است (OpenVPN/WireGuard) گزینهٔ آن را انتخاب کنید")

        self.run_action("تشخیص فیلترشکن", task)

    def action_apply_vpn(self):
        if not self._pin_gate():
            return
        name = self.vpn_choice.get()
        preset = next((p for p in t.all_presets() if p["name"] == name), None)
        if not preset:
            return
        http_port = self.http_port_var.get().strip()
        socks_port = self.socks_port_var.get().strip()
        custom = []
        for label, raw in (("HTTP", http_port), ("SOCKS", socks_port)):
            if raw and not (raw.isdigit() and 1 <= int(raw) <= 65535):
                self.log_line(f"پورت {label} نامعتبر است: «{raw}» — باید عدد بین ۱ تا ۶۵۵۳۵ باشد")
                return
            if raw:
                custom.append(f"{label}={raw}")

        def task():
            if not t.apply_vpn(preset["id"], http_port, socks_port):
                print("ذخیره نشد — مقدار پورت‌ها را ببین")
                return
            print(f"«{preset['name']}» ذخیره شد" + (f" (پورت سفارشی: {' · '.join(custom)})" if custom else ""))
            importlib.reload(t)
            print(f"  پروکسی HTTP : {t.PROXY or '— (حالت TUN، بدون پروکسی)'}")
            print(f"  پروکسی SOCKS: {t.SOCKS or '—'}")
            print(f"  API اپ VPN  : {t.MLM_API or '— (خاموش)'}")

        self.run_action("اعمال فیلترشکن", task)

    def action_watchdog(self):
        def task():
            pid = t.watchdog_pid()
            if pid:
                t.stop_watchdog()
                print(f"نظارت خودکار خاموش شد (PID {pid})")
            else:
                new_pid = t.start_watchdog()
                if new_pid:
                    print(f"نظارت خودکار روشن شد (PID {new_pid}) — هر ۲ دقیقه چک می‌کند")
                else:
                    print("اجرای نظارت ممکن نشد (pythonw پیدا نشد)")

        self.run_action("نظارت خودکار", task)

    def _config_stamp(self):
        try:
            info = os.stat(t.CONFIG_FILE)
            return (info.st_mtime_ns, info.st_size)
        except OSError:
            return None

    def _config_report(self):
        """Validation result as text (used by the check button and the auto-check)."""
        problems = t.validate_config()
        if not problems:
            return "همهٔ مقدارهای فایل تنظیمات معتبرند"
        lines, errors = [], 0
        for level, message in problems:
            lines.append(("خطا   » " if level == "error" else "هشدار » ") + message)
            errors += 1 if level == "error" else 0
        lines.append(f"{errors} مقدار نامعتبر است — تا اصلاح نشود اعمال نکن" if errors
                     else "خطایی نیست؛ فقط هشدار بالا را ببین")
        return "\n".join(lines)

    def _watch_config(self):
        """Auto-check the file every time it is saved (read-only, never applies)."""
        try:
            stamp = self._config_stamp()
            if stamp != self._cfg_stamp:
                self._cfg_stamp = stamp
                self.log_line("فایل تنظیمات ذخیره شد — چک خودکار:")
                self.log_line(self._config_report())
                self.log_line("برای اعمال، دکمهٔ «تنظیمات» ← «بله» را بزن")
        finally:
            self.root.after(2000, self._watch_config)

    def action_check_config(self):
        """Validate agtool.ini only: nothing is written, opened or applied."""
        def task():
            print(f"فایل: {t.CONFIG_FILE}")
            print(self._config_report())
            print("هیچ چیزی اعمال یا نوشته نشد (این دکمه فقط می‌خواند)")

        self.run_action("چک تنظیمات", task)

    def action_settings(self):
        """Open agtool.ini, then reload the module so new values take effect."""
        if not self._pin_gate():
            return
        try:
            os.startfile(t.CONFIG_FILE)
        except OSError as exc:
            self.log_line(f"باز کردن فایل تنظیمات ممکن نشد: {exc}")
            return
        self.log_line(f"فایل تنظیمات باز شد:\n  {t.CONFIG_FILE}")
        self.log_line("مقادیر را عوض کنید، ذخیره کنید، بعد دکمهٔ «بله» را بزنید تا بارگذاری شود.")
        if messagebox.askyesno("تنظیمات", "فایل را ذخیره کردی؟  «بله» = بازخوانی تنظیمات"):
            self.reload_config()

    def reload_config(self):
        if self.busy:
            self.log_line("الان یک عملیات در حال اجراست — کمی بعد دوباره تلاش کن")
            return
        problems = t.validate_config()
        errors = [msg for level, msg in problems if level == "error"]
        for level, msg in problems:
            if level == "warn":
                self.log_line(f"هشدار: {msg}")
        if errors:
            self.log_line("⚠ مقدارهای نامعتبر پیدا شد — هنوز اعمال نشده:")
            for msg in errors:
                self.log_line(f"  • {msg}")
            body = "این مقدارها در agtool.ini نامعتبرند:\n\n" + "\n".join(f"• {msg}" for msg in errors)
            body += "\n\nبا همین مقدارها باز هم اعمال شود؟\n«خیر» = تنظیمات فعلی دست‌نخورده می‌ماند"
            if not messagebox.askyesno("تنظیمات نامعتبر", body):
                self.log_line("تنظیمات قبلی حفظ شد — فایل را اصلاح کن و دوباره «اعمال» را بزن")
                return
            self.log_line("طبق انتخاب تو، با وجود مقدار نامعتبر اعمال شد")
        try:
            importlib.reload(t)
        except Exception as exc:
            self.log_line(f"بازخوانی تنظیمات ناموفق: {exc}")
            return
        self.log_line("تنظیمات جدید بارگذاری شد:")
        self.log_line(f"  پروکسی HTTP : {t.PROXY}")
        self.log_line(f"  پروکسی SOCKS: {t.SOCKS}")
        self.log_line(f"  API اپ VPN  : {t.MLM_API or '— (خاموش)'}")
        self.log_line(f"  اپ VPN      : {t.MLM_EXE or '— (خاموش)'}")
        self.log_line(f"  Antigravity : {t.IDE_EXE}")
        self.log_line(f"  قفل تعویض   : {t.MIN_SWITCH_SECONDS // 60} دقیقه")
        self.log_line(f"  اعلان       : {'روشن' if t.NOTIFY_ENABLED else 'خاموش'}")
        self._restart_watchdog()
        self.refresh()

    def _restart_watchdog(self):
        """The watchdog runs in its own process, so restart it to pick up new settings."""
        pid = t.watchdog_pid()
        if not pid:
            return
        t.stop_watchdog()
        time.sleep(1.5)  # let the old process release the single-instance mutex
        if t.watchdog_pid():
            self.log_line(f"نظارت خودکار (PID {pid}) بسته شد ولی هنوز پروسهٔ دیگری در حال اجراست")
            return
        new_pid = t.start_watchdog()
        if new_pid:
            self.log_line(f"نظارت خودکار با تنظیمات جدید ری‌استارت شد (PID جدید: {new_pid})")
        else:
            self.log_line("ری‌استارت نظارت خودکار ناموفق بود (pythonw پیدا نشد)")

    def action_autostart(self):
        def task():
            if os.path.exists(t.STARTUP_LNK):
                t.cmd_autostart("off")
            else:
                t.cmd_autostart("on")

        self.run_action("استارت خودکار با ویندوز", task)

    # ----------------------------------------------------- new tool buttons
    def action_doctor(self):
        self.run_action("عیب‌یاب", lambda: t.cmd_doctor())

    def action_clean(self):
        if not messagebox.askyesno("پاک‌سازی کش",
                                   "کش و لاگ آنتی‌گرویتی پاک شود؟\n\nپروژه‌ها، تنظیمات، اکستنشن‌ها و ورود دست‌نخورده می‌مانند."):
            return
        self.run_action("پاک‌سازی کش", lambda: t.cmd_clean(dry=False))

    def action_kill(self):
        if not messagebox.askyesno("بستن پروسه‌ها", "همهٔ پروسه‌های آنتی‌گرویتی (IDE و language_server) بسته شوند؟"):
            return
        self.run_action("بستن پروسه‌های IDE", lambda: t.cmd_kill())

    def action_nodes(self):
        self.run_action("تست سرعت نودها", lambda: t.cmd_nodes())

    def action_update(self):
        self.run_action("بروزرسانی برنامه", lambda: t.cmd_update(install=True))

    def action_launchvpn(self):
        self.run_action("اجرای فیلترشکن", lambda: t.cmd_launchvpn())

    def action_quota(self):
        self.run_action("چک اعتبار", lambda: t.cmd_quota(force=True))

    def action_bundle(self):
        def task():
            t.cmd_bundle()

        self.run_action("بستهٔ عیب‌یابی", task)

    def action_shortcut(self):
        self.run_action("شورتکات دسکتاپ IDE", lambda: t.cmd_shortcut())

    def action_envproxy(self):
        if not self._pin_gate():
            return
        current = (t.env_proxy_read() or {}).get("HTTP_PROXY")
        action = "off" if current else "on"

        def task():
            t.cmd_envproxy(action)

        self.run_action("پروکسی ترمینال/ابزارها", task)

    def action_profile_export(self):
        path = filedialog.asksaveasfilename(
            title="ذخیرهٔ پروفایل", defaultextension=".aitool-profile",
            initialfile=f"AI-Tool-{time.strftime('%Y%m%d')}.aitool-profile",
            filetypes=[("پروفایل AI Tool", "*.aitool-profile"), ("همهٔ فایل‌ها", "*.*")])
        if not path:
            return

        def task():
            t.cmd_profile("export", path)

        self.run_action("ذخیرهٔ پروفایل", task)

    def action_profile_import(self):
        if not self._pin_gate():
            return
        path = filedialog.askopenfilename(title="انتخاب فایل پروفایل",
                                          filetypes=[("پروفایل AI Tool", "*.aitool-profile"),
                                                     ("همهٔ فایل‌ها", "*.*")])
        if not path:
            return

        def task():
            if t.cmd_profile("import", path) != 0:
                return
            importlib.reload(t)
            print("تنظیمات تازه در همین پنجره اعمال شد")

        self.run_action("بازیابی پروفایل", task)
        self.root.after(2500, self._refresh_presets)

    def action_custom_vpn(self):
        """Any VPN client: pick the exe/shortcut, detect its proxy port, save it as a preset."""
        if not self._pin_gate():
            return
        path = filedialog.askopenfilename(
            title="فایل اجرایی یا شورتکات فیلترشکن را انتخاب کن",
            filetypes=[("برنامه یا شورتکات", "*.exe;*.lnk"), ("همهٔ فایل‌ها", "*.*")])
        if not path:
            return
        default_name = os.path.splitext(os.path.basename(path))[0]
        ports = simpledialog.askstring(
            "فیلترشکن دلخواه",
            "پورت HTTP و SOCKS را با فاصله بده\n(خالی = خودش تشخیص می‌دهد، که کمی طول می‌کشد):",
            parent=self.root)
        if ports is None:
            return
        raw = ports.replace("،", " ").replace(",", " ").split()
        http_port = raw[0] if raw else ""
        socks_port = raw[1] if len(raw) > 1 else ""

        def task():
            t.cmd_vpn_custom(default_name, path, http_port, socks_port)

        self.run_action("فیلترشکن دلخواه", task)
        self.root.after(3000, self._refresh_presets)

    def action_pin(self):
        if t.pin_is_set():
            if not messagebox.askyesno("قفل تنظیمات", "قفل روشن است.\n\n«بله» = خاموش کردن قفل   ·   «خیر» = عوض کردن PIN"):
                new_pin = simpledialog.askstring("قفل تنظیمات", "PIN تازه (حداقل ۴ رقم):",
                                                 show="●", parent=self.root)
                if not new_pin:
                    return
                old_pin = simpledialog.askstring("قفل تنظیمات", "PIN فعلی:", show="●", parent=self.root)
                if old_pin is None:
                    return

                def task():
                    problem = t.clear_pin(old_pin)
                    if problem:
                        print(problem)
                        return
                    problem = t.set_pin(new_pin)
                    print(problem or "PIN عوض شد")
                    importlib.reload(t)

                self.run_action("قفل تنظیمات", task)
                return
            pin = simpledialog.askstring("قفل تنظیمات", "PIN فعلی:", show="●", parent=self.root)
            if pin is None:
                return

            def unlock():
                problem = t.clear_pin(pin)
                print(problem or "قفل تنظیمات خاموش شد")
                importlib.reload(t)

            self.run_action("قفل تنظیمات", unlock)
            return
        new_pin = simpledialog.askstring("قفل تنظیمات", "PIN تازه (حداقل ۴ رقم):",
                                         show="●", parent=self.root)
        if not new_pin:
            return
        again = simpledialog.askstring("قفل تنظیمات", "همان PIN را دوباره بزن:",
                                      show="●", parent=self.root)
        if new_pin != again:
            self.log_line("دو PIN یکسان نبود — قفل روشن نشد")
            return

        def lock():
            problem = t.set_pin(new_pin)
            print(problem or "قفل تنظیمات روشن شد")
            importlib.reload(t)

        self.run_action("قفل تنظیمات", lock)


def main():
    try:  # crisp text on scaled displays
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    root = tk.Tk()
    App(root)
    # never open a window taller than the screen (small laptops, 125% scaling)
    height = min(960, root.winfo_screenheight() - 90)
    root.geometry(f"1020x{max(620, height)}")
    root.mainloop()


if __name__ == "__main__":
    main()
