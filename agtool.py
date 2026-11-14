#!/usr/bin/env python3
"""AI Tool — launcher/health-checker for Google (Chrome) and Antigravity over the MLM VPN proxy.

Usage:
    python agtool.py            -> interactive menu
    python agtool.py flow       -> open Google Flow in Chrome through the proxy
    python agtool.py ide        -> refresh token if needed + launch Antigravity with proxy
    python agtool.py status     -> proxy / node / account / quota status
    python agtool.py heal       -> pick a working MLM node and re-test the tunnel
    python agtool.py watch 120  -> watchdog: check the tunnel every 120s, switch node when it dies
    python agtool.py autostart on|off|status -> silent watchdog at Windows login
    python agtool.py preflight  -> read-only test before opening Flow/IDE (changes nothing)
    python agtool.py heal force -> allow a node switch even inside the safety cooldown
    python agtool.py history    -> report of IP changes and node switches
"""

import base64
import configparser
import ctypes
import datetime
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

# pythonw.exe (silent autostart) has no console: give print() somewhere to go
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w", encoding="utf-8")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w", encoding="utf-8")
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# when frozen (AI-Tool.exe) the app lives next to its exe, not inside the temp bundle
FROZEN = bool(getattr(sys, "frozen", False))
HERE = (os.path.dirname(os.path.abspath(sys.executable)) if FROZEN
        else os.path.dirname(os.path.abspath(__file__)))
CONFIG_FILE = os.path.join(HERE, "agtool.ini")

_DEFAULT_CONFIG = r"""; ============================================================
;  AI Tool — تنظیمات اتصال  (این فایل را با Notepad باز کنید)
;  فقط مقدارهای سمت راست را عوض کنید؛ خطی که با ; شروع می‌شود توضیح است.
;  بعد از ذخیره، برنامه را کامل ببندید و دوباره باز کنید.
; ============================================================

[vpn]
; پورت HTTP فیلترشکن (اکثر اپ‌ها همین را می‌دهند)
http_proxy = http://127.0.0.1:20809
; پورت SOCKS — اگر فیلترشکن شما این را ندارد، همان مقدار http_proxy را بگذارید
socks_proxy = socks5h://127.0.0.1:20808
; API محلی اپ VPN (برای روشن‌کردن خودکار و تعویض نود) — اگر ندارد خالی بگذارید
api = http://127.0.0.1:62569
; مسیر اپ VPN — اگر خالی باشد برنامه خودش اپ را اجرا نمی‌کند
app_exe = C:\Program Files\MLM VPN\MLM VPN.exe

[antigravity]
; مسیر Antigravity IDE
ide_exe = %LOCALAPPDATA%\Programs\Antigravity IDE\Antigravity IDE.exe
; کلاینت OAuth خود IDE — فقط اگر توکن خودکار تازه می‌شود لازم است
client_id =
client_secret =

[chrome]
; اگر کروم جای غیرمعمولی نصب است مسیر کامل را بنویسید؛ خالی = مسیرهای پیش‌فرض
exe =

[safety]
; حداقل فاصلهٔ مجاز بین دو تعویض نود (دقیقه) — تعویض نود = IP جدید = ریسک بن
switch_cooldown_minutes = 30

[ui]
; اعلان ویندوز: ۱ روشن ، ۰ خاموش
notifications = 1
; با باز شدن پنجره خودش به VPN وصل شود (بدون تغییر نود و IP): ۱ روشن ، ۰ خاموش
connect_on_start = 1

[watch]
; فاصلهٔ بررسی تونل توسط نظارت خودکار (ثانیه) — حداقل ۱۰
interval_seconds = 120

[update]
; نسخهٔ فعلی برنامه (دستی بالا ببر وقتی نسخهٔ تازه ساختی)
version = 1.1.0
; ریپوی گیت‌هاب به شکل owner/name — منبع رسمی نسخه‌ها و دکمهٔ به‌روزرسانی
repo =
; آدرس اینترنتی فایلی که نسخهٔ آخر را دارد (متن ساده مثل 1.1.0 یا JSON {"version":"1.1.0"})
; خالی = بررسی به‌روزرسانی خاموش
feed_url =

[quota]
; اگر اعتبار (credits) آنتی‌گرویتی کمتر از این عدد شد اعلان بده — ۰ = خاموش
warn_below = 50
; فاصلهٔ بررسی اعتبار (دقیقه)
interval_minutes = 60

[security]
; هش قفل تنظیمات — خالی = بدون قفل (با دکمهٔ «قفل تنظیمات» ساخته می‌شود)
pin_hash =
"""


def _load_config(path):
    """Read agtool.ini, creating it with commented defaults on first run."""
    if not os.path.exists(path):
        try:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(_DEFAULT_CONFIG)
        except OSError:
            pass
    parser = configparser.ConfigParser(interpolation=None)  # %LOCALAPPDATA% must survive
    try:
        parser.read(path, encoding="utf-8")
    except Exception:
        pass
    return parser


_CFG = _load_config(CONFIG_FILE)
_HAVE_CFG = os.path.exists(CONFIG_FILE)  # False only if the folder is read-only


def cfg(section, key, fallback=""):
    try:
        value = _CFG.get(section, key, fallback=fallback).strip()
    except Exception:
        return fallback
    return value or fallback


def cfg_opt(section, key):
    """Raw value without a fallback: an empty entry means "feature disabled"."""
    try:
        return _CFG.get(section, key, fallback="").strip()
    except Exception:
        return ""


def cfg_path(section, key, fallback=""):
    return os.path.expandvars(os.path.expanduser(cfg(section, key, fallback)))


# an empty http_proxy means TUN mode (OpenVPN/WireGuard): no local proxy at all
PROXY = cfg_opt("vpn", "http_proxy") if _HAVE_CFG else "http://127.0.0.1:20809"
SOCKS = cfg_opt("vpn", "socks_proxy") if _HAVE_CFG else "socks5h://127.0.0.1:20808"
# empty api/app_exe in the ini = that capability is switched off on purpose
MLM_API = cfg_opt("vpn", "api") if _HAVE_CFG else "http://127.0.0.1:62569"
MLM_EXE = (os.path.expandvars(cfg_opt("vpn", "app_exe")) if _HAVE_CFG
           else r"C:\Program Files\MLM VPN\MLM VPN.exe")
TOKEN_FILE = os.path.expanduser("~/.gemini/jetski-standalone-oauth-token")
WATCH_LOG = os.path.join(HERE, "agtool-watch.log")
STARTUP_DIR = os.path.join(os.environ.get("APPDATA", ""), r"Microsoft\Windows\Start Menu\Programs\Startup")
STARTUP_LNK = os.path.join(STARTUP_DIR, "AI Tool Watchdog.lnk")
MUTEX_NAME = "Local\\agtool_watchdog"
STATE_FILE = os.path.join(HERE, "agtool-state.json")
# Switching nodes = new exit IP = ban risk for the Google/Antigravity accounts.
# So node switching is rate-limited; reconnecting the SAME node is always allowed.
try:
    _cfg_switch_min = int(cfg("safety", "switch_cooldown_minutes", "30"))
except ValueError:
    _cfg_switch_min = 30
_switch_env = os.environ.get("AG_MIN_SWITCH", "")
MIN_SWITCH_SECONDS = int(_switch_env) if _switch_env.isdigit() else max(1, _cfg_switch_min) * 60
# OAuth client of the IDE; the secret is NOT shipped in the source — it comes from the
# local install (agtool.ini → [antigravity] client_secret) or the AG_CLIENT_SECRET env var.
CLIENT_ID = cfg("antigravity", "client_id", "").strip()
CLIENT_SECRET = (cfg("antigravity", "client_secret", "")
                 or os.environ.get("AG_CLIENT_SECRET", "")).strip()
IDE_EXE = cfg_path("antigravity", "ide_exe",
                   r"%LOCALAPPDATA%\Programs\Antigravity IDE\Antigravity IDE.exe")
CHROME_CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
]
_CHROME_CFG = cfg_path("chrome", "exe", "")
if _CHROME_CFG:
    CHROME_CANDIDATES.insert(0, _CHROME_CFG)
FLOW_URLS = ["https://labs.google/flow", "https://www.google.com/"]
SERVICE = "exa.language_server_pb.LanguageServerService"

GREEN, RED, YEL, DIM, END = "\033[92m", "\033[91m", "\033[93m", "\033[2m", "\033[0m"


def ok(msg):
    print(f"  {GREEN}OK{END}   {msg}")


def bad(msg):
    print(f"  {RED}FAIL{END} {msg}")


def warn(msg):
    print(f"  {YEL}WARN{END} {msg}")


def info(msg):
    print(f"  {DIM}{msg}{END}")


# ---------------------------------------------------------------- http helpers
def opener(use_proxy=True):
    proxy_map = {"http": PROXY, "https": PROXY} if (use_proxy and PROXY) else {}
    return urllib.request.build_opener(urllib.request.ProxyHandler(proxy_map))


def proxy_ports_open():
    """True when both local proxy ports of the VPN client accept connections."""
    http_port, socks_port = proxy_ports()
    return bool(http_port and socks_port and port_open(http_port) and port_open(socks_port))


def proxy_ports():
    """(http_port, socks_port) taken from the configured URLs."""
    def port(url):
        try:
            return urllib.parse.urlparse(url).port
        except Exception:
            return None
    return port(PROXY), port(SOCKS)


def chrome_proxy_args():
    """Chrome flags needed for this mode (none when the VPN works at TUN level)."""
    return [f"--proxy-server={PROXY}", "--disable-quic"] if PROXY else []


def http_status(url, use_proxy=True, timeout=25):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/142"})
        with opener(use_proxy).open(req, timeout=timeout) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception:
        return 0


def port_open(port, host="127.0.0.1"):
    s = socket.socket()
    s.settimeout(1.5)
    try:
        return s.connect_ex((host, port)) == 0
    finally:
        s.close()


PROXY_SCHEMES = ("http", "https", "socks5", "socks5h", "socks4", "socks4a")


def validate_config(path=None):
    """Check agtool.ini before it is applied. Returns [(level, message)] with
    level "error" (value is unusable) or "warn" (works, but looks wrong)."""
    path = path or CONFIG_FILE
    problems = []
    parser = configparser.ConfigParser(interpolation=None)
    try:
        if not parser.read(path, encoding="utf-8"):
            return [("error", f"فایل تنظیمات خوانده نشد: {path}")]
    except configparser.Error as exc:
        return [("error", f"ساختار فایل تنظیمات خراب است: {exc}")]

    def raw(section, key):
        return parser.get(section, key, fallback="").strip()

    def check_url(value, label, schemes, empty_note=""):
        if not value:
            if empty_note:
                problems.append(("warn", empty_note))
            return
        parsed = urllib.parse.urlparse(value)
        if parsed.scheme.lower() not in schemes:
            expected = " یا ".join(f"{s}://" for s in schemes[:2])
            problems.append(("error", f"{label} باید با {expected} شروع شود — مقدار فعلی: {value}"))
            return
        if not parsed.hostname:
            problems.append(("error", f"{label} آدرس (میزبان) ندارد — مقدار فعلی: {value}"))
            return
        try:
            port = parsed.port
        except ValueError:
            port = None  # empty, non-numeric, or out of range
        if port is None:
            problems.append(("error", f"{label} پورت ندارد یا عدد نیست — مقدار فعلی: {value}"))
        elif not 1 <= port <= 65535:
            problems.append(("error", f"{label} پورت باید بین ۱ و ۶۵۵۳۵ باشد — مقدار فعلی: {port}"))
        if parsed.hostname not in ("127.0.0.1", "localhost", "::1"):
            problems.append(("warn", f"{label} به «{parsed.hostname}» اشاره می‌کند، نه همین کامپیوتر"))

    def check_int(value, label, minimum=0, maximum=None):
        if value == "":
            problems.append(("warn", f"{label} خالی است — مقدار پیش‌فرض استفاده می‌شود"))
            return
        try:
            number = int(value)
        except ValueError:
            problems.append(("error", f"{label} باید عدد باشد — مقدار فعلی: {value}"))
            return
        if number < minimum:
            problems.append(("error", f"{label} نباید کمتر از {minimum} باشد — مقدار فعلی: {number}"))
        elif maximum is not None and number > maximum:
            problems.append(("error", f"{label} نباید بیشتر از {maximum} باشد — مقدار فعلی: {number}"))

    def check_path(value, label):
        if not value:
            return
        expanded = os.path.expandvars(os.path.expanduser(value))
        if not os.path.exists(expanded):
            problems.append(("warn", f"{label} در این مسیر پیدا نشد: {expanded}"))

    if not parser.has_section("vpn"):
        problems.append(("error", "بخش [vpn] در فایل تنظیمات نیست"))
        return problems

    check_url(raw("vpn", "http_proxy"), "پروکسی HTTP", PROXY_SCHEMES,
              empty_note="پروکسی HTTP خالی است ⇒ حالت TUN (VPN سیستمی) فعال می‌شود")
    check_url(raw("vpn", "socks_proxy"), "پروکسی SOCKS", PROXY_SCHEMES,
              empty_note="پروکسی SOCKS خالی است؛ بعضی بررسی‌ها فقط با HTTP کار می‌کنند")
    check_url(raw("vpn", "api"), "API اپ VPN", ("http", "https"))
    check_path(raw("vpn", "app_exe"), "مسیر اپ VPN")
    check_path(raw("antigravity", "ide_exe"), "مسیر Antigravity")
    check_path(raw("chrome", "exe"), "مسیر کروم")
    check_int(raw("safety", "switch_cooldown_minutes"), "قفل تعویض نود (دقیقه)")
    check_int(raw("ui", "notifications"), "اعلان ویندوز (۰/۱)", 0, 1)
    check_int(raw("watch", "interval_seconds"), "فاصلهٔ نظارت (ثانیه)", 10)
    return problems


# ---------------------------------------------------------------- MLM node
# console children of pythonw would otherwise pop a black window on Windows
NOWIN = 0x08000000  # CREATE_NO_WINDOW


def mlm_api_port():
    try:
        return urllib.parse.urlparse(MLM_API).port or 62569
    except Exception:
        return 62569


def mlm_running():
    if MLM_API and port_open(mlm_api_port()):
        return True
    try:
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq MLM VPN.exe", "/NH"],
                             capture_output=True, text=True, timeout=20,
                             creationflags=NOWIN).stdout
        return "MLM VPN.exe" in (out or "")
    except Exception:
        return False


def focus_app_window(exe_name, tries=10, delay=1.0):
    """Bring the VPN client's own window to the front so the user can see it."""
    name = os.path.splitext(os.path.basename(exe_name or ""))[0].replace("'", "''")
    if not name:
        return False
    script = (
        "Add-Type -TypeDefinition 'using System; using System.Runtime.InteropServices; "
        "public class WinFocus { [DllImport(\"user32.dll\")] public static extern bool SetForegroundWindow(IntPtr h); "
        "[DllImport(\"user32.dll\")] public static extern bool ShowWindow(IntPtr h, int c); }'; "
        f"$p = Get-Process -Name '{name}' -ErrorAction SilentlyContinue | "
        "Where-Object { $_.MainWindowHandle -ne 0 } | Select-Object -First 1; "
        "if ($p) { [WinFocus]::ShowWindow($p.MainWindowHandle, 9) | Out-Null; "
        "[WinFocus]::SetForegroundWindow($p.MainWindowHandle) | Out-Null; 'found' } else { 'none' }")
    for _ in range(max(1, tries)):
        try:
            out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                                 capture_output=True, text=True, timeout=30,
                                 creationflags=NOWIN).stdout.strip()
        except Exception:
            out = ""
        if out == "found":
            return True
        time.sleep(delay)
    return False


PROGRESS = None  # callable(percent, text) — the GUI uses it to draw a real progress bar


def report_progress(percent, text=""):
    """Tell the GUI how far along a long action is (percent, short Persian step label)."""
    fn = PROGRESS
    if fn is None:
        return
    try:
        fn(max(0, min(100, int(percent))), text)
    except Exception:
        pass


def start_mlm(wait=75, progress_from=None, progress_to=None):
    """Launch the VPN desktop app, show its window first, then wait for its local API."""
    if mlm_running():
        ok("اپ VPN از قبل اجراست")
        return True
    if not MLM_EXE:
        bad("مسیر اپ VPN در agtool.ini تنظیم نشده (بخش vpn → app_exe)")
        return False
    if not os.path.exists(MLM_EXE):
        bad(f"اپ VPN در این مسیر نیست: {MLM_EXE}")
        return False
    print("  پنجرهٔ خود اپ VPN باز می‌شود؛ اگر ویندوز «تأیید مدیر» خواست «بله» بزن ...")
    elevated = False
    try:
        subprocess.Popen([MLM_EXE], cwd=os.path.dirname(MLM_EXE), creationflags=0x00000008)
    except OSError as e:
        if getattr(e, "winerror", None) == 740:  # needs elevation
            warn("اجرای اپ به دسترسی مدیر نیاز دارد — تایید پنجرهٔ UAC")
            elevated = True
            subprocess.Popen(["powershell", "-NoProfile", "-Command",
                              f"Start-Process -FilePath '{MLM_EXE}' -Verb RunAs"],
                             creationflags=0x00000008)
        else:
            bad(f"اجرای اپ ممکن نشد: {e}")
            return False
    if focus_app_window(os.path.basename(MLM_EXE), tries=8):
        ok("پنجرهٔ اپ VPN باز شد و جلو آمد")
    else:
        info("پنجرهٔ اپ VPN هنوز بالا نیامده — منتظر می‌مانم (اگر پنجرهٔ تأیید ویندوز منتظرت است، همان را تأیید کن)")
    started = time.time()
    deadline = started + wait
    next_note = started + 15
    while time.time() < deadline:
        if progress_from is not None and progress_to is not None:
            span = progress_to - progress_from
            report_progress(progress_from + span * (time.time() - started) / float(wait),
                            "بالا آمدن اپ VPN")
        if MLM_API and port_open(mlm_api_port()):
            ok(f"اپ VPN بالا آمد (API روی {mlm_api_port()})")
            if progress_to is not None:
                report_progress(progress_to, "اپ VPN بالا آمد")
            return True
        if elevated and time.time() >= next_note:
            info("منتظر تأیید پنجرهٔ ویندوز (UAC) و بالا آمدن اپ ...")
            next_note = time.time() + 20
        time.sleep(2)
    bad("اپ VPN بالا نیامد")
    if elevated:
        info("اگر پنجرهٔ UAC را نبستی: اپ VPN را دستی باز کن، یک بار وصل شو، بعد «اتصال خودکار» را بزن")
    else:
        info("پورت API را در agtool.ini بررسی کن (بخش vpn → api)")
    return False


def mlm_start(uri):
    if not MLM_API:
        return "ERR api-not-configured"
    body = json.dumps({"uri": uri, "useSystemProxy": True, "solo": True}).encode()
    req = urllib.request.Request(MLM_API + "/api/v2ray/start", data=body,
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with opener(False).open(req, timeout=90) as r:
            return r.read().decode("utf8", "replace")
    except Exception as e:
        return "ERR " + str(e)


def load_nodes():
    candidates = [os.path.join(HERE, "mlm_found_nodes.json")]
    bundle = getattr(sys, "_MEIPASS", "")
    if bundle:
        candidates.append(os.path.join(bundle, "mlm_found_nodes.json"))
    nodes = []
    for path in candidates:
        try:
            nodes = json.load(open(path, encoding="utf8"))
            break
        except Exception:
            continue
    if not nodes:
        return []
    nodes.sort(key=lambda n: n.get("delay") or 99999)
    return nodes


def tunnel_ok():
    return http_status("https://www.google.com/generate_204", timeout=20) == 204


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(st):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, indent=1)
    except OSError:
        pass


def node_name(n):
    frag = urllib.parse.unquote((n.get("uri") or "").split("#")[-1])
    return frag.split("·")[-1].strip() or n.get("host") or n.get("id", "")


def adopt_winner():
    """Seed the state with the node that is really connected (name recorded in mlm_winner.json)."""
    st = load_state()
    if (st.get("node") or {}).get("uri"):
        return st
    try:
        with open(os.path.join(HERE, "mlm_winner.json"), encoding="utf-8") as f:
            w = json.load(f)
    except Exception:
        return st
    for n in load_nodes():
        if node_name(n) == w.get("name"):
            st["node"] = {"name": w.get("name"), "uri": n["uri"]}
            st.setdefault("last_switch", 0)
            save_state(st)
            break
    return st


_last_notify = {}
NOTIFY_ENABLED = os.environ.get("AG_NOTIFY", cfg("ui", "notifications", "1")) != "0"
# bring the tunnel up as soon as the window opens (never switches node)
AUTO_CONNECT = os.environ.get("AG_AUTOCONNECT", cfg("ui", "connect_on_start", "1")).lower() \
    not in ("0", "no", "false", "off")


def notify(title, msg, minutes=5):
    """Windows tray notification (NotifyIcon balloon) — no extra packages needed."""
    if not NOTIFY_ENABLED:
        return
    key = (title, msg)
    now = time.time()
    if now - _last_notify.get(key, 0) < minutes * 60:  # do not spam the same message
        return
    _last_notify[key] = now
    script = "\n".join([
        "Add-Type -AssemblyName System.Windows.Forms, System.Drawing",
        "try { Add-Type -TypeDefinition 'using System.Runtime.InteropServices; public class DpiFix { "
        "[DllImport(\"user32.dll\")] public static extern bool SetProcessDPIAware(); }'; "
        "[void][DpiFix]::SetProcessDPIAware() } catch {}",
        "$ni = New-Object System.Windows.Forms.NotifyIcon",
        "$ni.Icon = [System.Drawing.SystemIcons]::Information",
        "$ni.Text = 'AI Tool'",
        "$ni.BalloonTipTitle = '" + str(title).replace("'", "''") + "'",
        "$ni.BalloonTipText = '" + str(msg).replace("'", "''") + "'",
        "$ni.Visible = $true",
        "$ni.ShowBalloonTip(10000)",
        "$t = New-Object System.Windows.Forms.Timer",
        "$t.Interval = 12000",
        "$t.Add_Tick({ $t.Stop(); $ni.Visible = $false; $ni.Dispose(); [System.Windows.Forms.Application]::Exit() })",
        "$t.Start()",
        "[System.Windows.Forms.Application]::Run()",
    ])
    try:
        encoded = base64.b64encode(script.encode("utf-16-le")).decode()
        subprocess.Popen(["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-EncodedCommand", encoded],
                         creationflags=0x08000000)  # CREATE_NO_WINDOW
    except Exception:
        pass


def remember_ip(ip, context="auto", node=""):
    if not ip:
        return
    st = load_state()
    prev = st.get("last_ip")
    st["last_ip"] = ip
    st["last_ip_ts"] = int(time.time())
    if prev and prev != ip:
        info = geo_for_ip(ip) or {}
        st.setdefault("ip_history", []).append({"from": prev, "to": ip, "ts": int(time.time()),
                                                "cc": info.get("cc", ""),
                                                "country": info.get("name", "")})
        st["ip_history"] = st["ip_history"][-20:]
        country = history_country(ip=ip)
        if context == "switch":
            notify("AI Tool — تعویض نود",
                   f"نود: {node}\nIP جدید: {ip} (قبلی: {prev})" + (f"\nکشور: {country}" if country else ""))
        else:
            notify("AI Tool — تغییر IP", f"{prev} → {ip}" + (f"\nکشور: {country}" if country else ""))
    save_state(st)


def switch_cooldown_left():
    return max(0, MIN_SWITCH_SECONDS - (time.time() - (load_state().get("last_switch") or 0)))


def heal_node(limit=12, force=False):
    """Bring the tunnel back with the smallest possible IP change.

    1) reconnect the SAME node (usually keeps the same exit IP),
    2) only if that fails and the cooldown has passed, try other nodes.
    """
    if not PROXY:
        bad("در حالت VPN سیستمی (TUN) تعویض نود توسط این ابزار انجام نمی‌شود")
        info("نود را از خود اپ VPN عوض کنید")
        return False
    if not MLM_API:
        bad("API اپ VPN در agtool.ini تنظیم نشده — تعویض/اتصال نود ممکن نیست")
        info("بخش [vpn] → api را پر کنید یا خودتان نود را در اپ VPN انتخاب کنید")
        return False
    report_progress(5, "بررسی وضعیت تونل")
    st = adopt_winner()
    cur = st.get("node") or {}
    print("  بازگرداندن تونل با کمترین تغییر ...")
    if cur.get("uri"):
        print(f"       ↻ اتصال دوباره به همان نود: {cur.get('name')} (IP نباید عوض شود)")
        report_progress(15, f"اتصال دوباره به {cur.get('name')}")
        mlm_start(cur["uri"])
        time.sleep(6)
        report_progress(30, "تست همان نود")
        if tunnel_ok():
            ip = exit_ip()
            remember_ip(ip)
            report_progress(100, "همان نود برگشت")
            ok(f"همان نود برگشت — IP: {ip}")
            return True
        warn("همان نود جواب نداد")
    left = switch_cooldown_left()
    report_progress(35, "بررسی قفل تعویض نود")
    if left > 0 and not force:
        bad(f"تعویض نود قفل است: {int(left // 60)} دقیقهٔ دیگر (محافظت از بن اکانت)")
        info("عبور دستی: python agtool.py heal force")
        return False
    nodes = [n for n in load_nodes() if n.get("uri") != cur.get("uri")]
    if not nodes:
        bad("فهرست نود سالم در mlm_found_nodes.json نیست")
        info("پروفایل نودهای خودت را با «بازیابی پروفایل» وارد کن")
        return False
    print(f"  تست حداکثر {limit} نود دیگر ...")
    trial = nodes[:limit]
    for index, n in enumerate(trial):
        name = node_name(n)
        report_progress(40 + int(50 * index / max(1, len(trial))), f"تست نود {name}")
        res = mlm_start(n["uri"])
        time.sleep(5)
        if tunnel_ok():
            ip = exit_ip()
            st = load_state()
            st["node"] = {"name": name, "uri": n["uri"], "switched_at": int(time.time())}
            st["last_switch"] = time.time()
            geo = geo_for_ip(ip) or {}
            st.setdefault("switches", []).append({"ts": int(time.time()), "name": name, "ip": ip,
                                                  "cc": geo.get("cc", ""),
                                                  "country": geo.get("name", "")})
            st["switches"] = st["switches"][-20:]
            st["switch_count"] = int(st.get("switch_count") or 0) + 1
            save_state(st)
            remember_ip(ip, context="switch", node=name)
            json.dump({"name": name, "delay": n.get("delay"), "exit_ip": ip,
                       "host": (n.get("uri") or "").split("@")[-1].split("?")[0].split(":")[0],
                       "protocol": n.get("protocol")}, open(os.path.join(HERE, "mlm_winner.json"), "w", encoding="utf8"))
            report_progress(100, "نود تازه وصل شد")
            ok(f"نود تازه: {name} | IP جدید: {ip}")
            warn("حالا ۳۰ دقیقه نود را دستی عوض نکن (نود عوض شد = IP عوض شد)")
            return True
        print(f"       {name} → کار نکرد")
    bad("هیچ نودی جواب نداد")
    return False


def mlm_api_ok():
    """The VPN app's local control API answers (a running process alone is not enough)."""
    return bool(MLM_API) and port_open(mlm_api_port())


def mlm_listening_ports():
    """Local TCP ports the VPN app (or a child it spawned) is listening on, API excluded."""
    exe = os.path.basename(MLM_EXE) if MLM_EXE else "MLM VPN.exe"
    pids = pids_for_exe(exe)
    if not pids:
        return []
    listeners = listeners_by_pid()
    return sorted({port for pid in pids for port in listeners.get(pid, set())
                   if port != mlm_api_port()})


def adopt_mlm_ports():
    """The app may listen on other proxy ports than the config says (other version/port
    setting). Find the real ones and use them for this run, saving them in agtool.ini."""
    global PROXY, SOCKS
    if not mlm_running():
        return False
    ports = mlm_listening_ports()
    if not ports:
        bad("اپ VPN باز است ولی هیچ پورتی باز نکرده — یعنی خودش هنوز وصل نشده")
        info("داخل پنجرهٔ خود MLM یک نود انتخاب کن و «اتصال» را بزن (اگر اکانت/حجم ندارد، همان را درست کن)")
        return False
    info("پورت‌های محلی اپ VPN: " + ", ".join(str(p) for p in ports))
    http_ok = [p for p in ports[:8] if probe_http_proxy(p, timeout=5) in (200, 204)]
    socks_ok = [p for p in ports[:8] if probe_socks5(p)]
    if not http_ok and not socks_ok:
        bad("پورت‌های اپ VPN به‌عنوان پروکسی جواب ندادند — داخل خود MLM یک بار وصل شو")
        return False
    http_port = http_ok[0] if http_ok else (socks_ok[0] if socks_ok else None)
    socks_port = next((p for p in socks_ok if p != http_port), http_port)
    PROXY = f"http://127.0.0.1:{http_port}" if http_port else ""
    SOCKS = f"socks5h://127.0.0.1:{socks_port}" if socks_port else ""
    write_ini_values("vpn", {"http_proxy": PROXY, "socks_proxy": SOCKS})
    ok(f"پورت پروکسی اپ VPN پیدا شد و ثبت شد: http {http_port} · socks {socks_port}")
    return True


def reconnect_same_node():
    """Reconnect the recorded node through the VPN app API — the exit IP stays the same."""
    if not (MLM_API and PROXY):
        return False
    if not mlm_api_ok():
        info("API اپ VPN جواب نمی‌دهد — اتصال خودکار از راه API ممکن نیست")
        return False
    st = adopt_winner()
    cur = st.get("node") or {}
    if not cur.get("uri"):
        info("نود ذخیره‌شده‌ای نیست — یک بار نود را در اپ VPN انتخاب کن")
        return False
    info(f"اتصال دوباره به همان نود: {cur.get('name') or '؟'} (IP نباید عوض شود)")
    mlm_start(cur["uri"])
    time.sleep(6)
    if tunnel_ok():
        remember_ip(exit_ip())
        return True
    return False


def connect_best_node():
    """First-ever connection (no history yet): use the saved winner or the fastest known node."""
    if not (MLM_API and PROXY):
        return False
    if not mlm_api_ok():
        bad(f"API اپ VPN روی پورت {mlm_api_port()} جواب نمی‌دهد — اپ باز است ولی موتور اتصالش بالا نیامده")
        info("داخل پنجرهٔ MLM یک بار «اتصال» بزن؛ اگر خود MLM وصل نمی‌شود مشکل اکانت/حجم/پلن خودش است")
        return False
    if not load_nodes():
        bad("فهرست نود خالی است — این نسخه نودها را همراه ندارد")
        info("دکمهٔ «بازیابی پروفایل» را بزن و فایل پروفایل خودت را وارد کن، یا داخل خود MLM یک نود انتخاب کن")
        return False
    candidates = []
    try:
        with open(os.path.join(HERE, "mlm_winner.json"), encoding="utf8") as handle:
            candidates.append(json.load(handle))
    except Exception:
        pass
    candidates += load_nodes()
    seen = set()
    total = max(1, len(candidates))
    for index, node in enumerate(candidates):
        uri = node.get("uri")
        if not uri or uri in seen:
            continue
        seen.add(uri)
        name = node.get("name") or node_name(node)
        report_progress(72 + int(24 * index / total), f"تست نود {name}")
        info(f"اتصال به نود: {name}")
        mlm_start(uri)
        time.sleep(6)
        if tunnel_ok():
            st = load_state()
            st["node"] = {"name": name, "uri": uri, "switched_at": int(time.time())}
            st["last_switch"] = time.time()
            save_state(st)
            ip = exit_ip()
            remember_ip(ip)
            ok(f"نود انتخابشد: {name}" + (f" · IP: {ip} · {geo_text(ip)}" if ip else ""))
            info("اگر نود دیگری میخواهی، از داخل خود MLM VPN عوضش کن؛ بعد دکمهٔ «اتصال خودکار» را بزن")
            notify("AI Tool — اولین اتصال",
                   f"نود: {name}" + (f"\nIP: {ip}" if ip else "")
                   + "\nبرای عوض کردن نود، از داخل خود MLM VPN انجامش بده")
            report_progress(97, "نود انتخاب شد")
            return True
    return False


def cmd_connect():
    """Open the app, bring the tunnel up and keep the exit IP the same.

    Startup path of the GUI: start the VPN app if it is closed, reconnect the
    saved node, and never switch to another node (that would change the IP).
    """
    print("\n── اتصال خودکار ─────────────────────")
    report_progress(3, "شروع اتصال")
    if not PROXY:
        report_progress(15, "بررسی اینترنت و VPN سیستمی")
        if tunnel_ok():
            ok("حالت TUN — اینترنت از VPN رد می‌شود")
            ip = exit_ip()
            if ip:
                remember_ip(ip)
                ok(f"IP خروجی: {ip} · {geo_text(ip)}")
            return 0
        bad("حالت TUN: اینترنت جواب نداد — VPN سیستمی را وصل کن")
        return 1
    report_progress(12, "بررسی تونل فعلی")
    if tunnel_ok():
        ip = exit_ip()
        report_progress(100, "تونل از قبل سالم بود")
        ok("تونل از قبل سالم بود" + (f" — IP: {ip} · {geo_text(ip)}" if ip else ""))
        if ip:
            remember_ip(ip)
        return 0
    if not mlm_running():
        info("اپ VPN خاموش است → اول پنجرهٔ خودش باز می‌شود، بعد اتصال تست می‌شود")
        report_progress(20, "باز کردن اپ VPN")
        if not start_mlm(progress_from=20, progress_to=45):
            bad("اتصال خودکار نتوانست اپ VPN را بالا بیاورد")
            info("اپ VPN را دستی باز کن و یک بار وصل شو؛ بعد همین دکمه را بزن")
            return 1
        for step in range(10):  # the app needs a moment before its proxy ports listen
            report_progress(45 + step * 2, "انتظار برای پورت‌های پروکسی")
            if proxy_ports_open():
                break
            time.sleep(2)
    if not proxy_ports_open():
        report_progress(58, "جست‌وجوی پورت پروکسی اپ")
        adopt_mlm_ports()
    report_progress(62, "تست عبور ترافیک")
    if proxy_ports_open() and tunnel_ok():
        ip = exit_ip()
        if ip:
            remember_ip(ip)
        report_progress(100, "متصل شد")
        ok(f"متصل شد — IP: {ip}" + (f" · {geo_text(ip)}" if ip else ""))
        return 0
    report_progress(68, "اتصال دوباره به همان نود")
    if MLM_API and reconnect_same_node():
        ip = exit_ip()
        report_progress(100, "متصل شد")
        ok(f"متصل شد — IP: {ip}" + (f" · {geo_text(ip)}" if ip else ""))
        return 0
    if not (load_state().get("node") or {}).get("uri"):
        info("اولین اتصال است — یک نود از فهرست انتخاب می‌شود")
        report_progress(72, "انتخاب اولین نود")
        if connect_best_node():
            report_progress(100, "متصل شد")
            ip = exit_ip()
            node = (load_state().get("node") or {}).get("name") or "نامشخص"
            ok(f"اولین اتصال با نود {node} — IP: {ip}" + (f" · {geo_text(ip)}" if ip else ""))
            info("اگر این نود را نمی‌خواهی، داخل خود MLM VPN نود دیگری انتخاب کن و بعد «اتصال خودکار» را بزن")
            return 0
    bad("اتصال خودکار ممکن نشد")
    if mlm_running() and not proxy_ports_open():
        info("اپ VPN باز است ولی پروکسی‌اش بالا نیامده — پنجرهٔ خود MLM را بیاور و «اتصال» را بزن")
        info("اگر خود MLM هم وصل نمی‌شود: اکانت/حجم/پلن MLM را چک کن (این مورد مربوط به MLM است، نه این ابزار)")
    elif not mlm_api_ok():
        info(f"API اپ VPN روی پورت {mlm_api_port()} بسته است — اپ را کامل ببند و دوباره باز کن")
    else:
        info("در اپ VPN یک نود انتخاب کن، بعد دکمهٔ «تعمیر تونل» یا «تست دستی» را بزن")
    return 1


def ensure_tunnel(auto_heal=True):
    """Make sure the VPN app is running, its proxy ports are up and traffic actually passes."""
    if not PROXY:
        # TUN-style VPN (OpenVPN/WireGuard): no local proxy, traffic is routed by the OS
        if tunnel_ok():
            ok("حالت VPN سیستمی (TUN) — اینترنت از VPN رد می‌شود")
            remember_ip(exit_ip())
            return True
        bad("حالت TUN: اینترنت مستقیم جواب نداد (VPN وصل است؟)")
        return False
    if not mlm_running():
        warn("اپ MLM VPN خاموش است")
        if not start_mlm():
            return False
    if not (port_open(20809) and port_open(20808)):
        info("انتظار برای بالا آمدن پورت‌های پروکسی ...")
        for _ in range(45):
            if port_open(20809) and port_open(20808):
                break
            time.sleep(2)
        else:
            bad("پورت‌های پروکسی 20808/20809 بالا نیامدند")
            if not (auto_heal and heal_node()):
                return False
    if tunnel_ok():
        ok("تونل پروکسی سالم است (google 204)")
        remember_ip(exit_ip())
        return True
    bad("تونل پروکسی جواب نداد — دو بار دیگر تست می‌کنم (بدون تعویض نود)")
    for _ in range(2):
        time.sleep(4)
        if tunnel_ok():
            ok("تونل پروکسی سالم است (google 204)")
            remember_ip(exit_ip())
            return True
    return heal_node() if auto_heal else False


def exit_ip():
    try:
        with opener(True).open("https://api.ipify.org", timeout=20) as r:
            return r.read().decode().strip()
    except Exception:
        return ""


# ---------------------------------------------------------------- country of the exit IP
COUNTRY_FA = {
    "DE": "آلمان", "NL": "هلند", "FR": "فرانسه", "GB": "بریتانیا", "US": "آمریکا",
    "CA": "کانادا", "TR": "ترکیه", "AE": "امارات", "FI": "فنلاند", "SE": "سوئد",
    "NO": "نروژ", "DK": "دانمارک", "CH": "سوئیس", "AT": "اتریش", "BE": "بلژیک",
    "ES": "اسپانیا", "IT": "ایتالیا", "PT": "پرتغال", "PL": "لهستان", "CZ": "چک",
    "RO": "رومانی", "BG": "بلغارستان", "RU": "روسیه", "UA": "اوکراین", "HU": "مجارستان",
    "GR": "یونان", "IE": "ایرلند", "IS": "ایسلند", "LU": "لوکزامبورگ", "EE": "استونی",
    "LV": "لتونی", "LT": "لیتوانی", "SK": "اسلواکی", "SI": "اسلوونی", "HR": "کرواسی",
    "RS": "صربستان", "MD": "مولداوی", "AM": "ارمنستان", "GE": "گرجستان", "AZ": "آذربایجان",
    "KZ": "قزاقستان", "QA": "قطر", "KW": "کویت", "SA": "عربستان", "BH": "بحرین",
    "OM": "عمان", "JO": "اردن", "LB": "لبنان", "IL": "اسرائیل", "EG": "مصر",
    "MA": "مراکش", "TN": "تونس", "ZA": "آفریقای جنوبی", "IN": "هند", "PK": "پاکستان",
    "JP": "ژاپن", "KR": "کره جنوبی", "CN": "چین", "HK": "هنگ‌کنگ", "SG": "سنگاپور",
    "MY": "مالزی", "ID": "اندونزی", "TH": "تایلند", "VN": "ویتنام", "PH": "فیلیپین",
    "AU": "استرالیا", "NZ": "نیوزیلند", "BR": "برزیل", "AR": "آرژانتین", "CL": "شیلی",
    "MX": "مکزیک", "CO": "کلمبیا", "IR": "ایران", "CY": "قبرس", "MT": "مالت",
    "AL": "آلبانی", "MK": "مقدونیه", "BA": "بوسنی", "ME": "مونته‌نگرو", "XK": "کوزوو",
    "BY": "بلاروس", "KG": "قرقیزستان", "UZ": "ازبکستان", "TW": "تایوان", "NP": "نپال",
    "BD": "بنگلادش", "LK": "سری‌لانکا", "NG": "نیجریه", "KE": "کنیا", "MU": "موریس",
    "SC": "سیشل", "CR": "کاستاریکا", "PA": "پاناما", "UY": "اروگوئه", "PE": "پرو",
}


def flag_emoji(code):
    """Two-letter code → regional-indicator flag (falls back to the letters on Windows)."""
    code = (code or "").upper()
    if len(code) != 2 or not code.isalpha():
        return ""
    return "".join(chr(0x1F1E6 + ord(ch) - ord("A")) for ch in code)


def node_country_code(name):
    """Country code from a node name like «آلمان · DE-DVJP»."""
    match = re.search(r"\b([A-Z]{2})(?=[-_ ]|$)", name or "")
    if match and match.group(1) in COUNTRY_FA:
        return match.group(1)
    for code, persian in COUNTRY_FA.items():
        if persian and persian in (name or ""):
            return code
    return ""


def _geo_fetch(ip):
    """Network lookup only (no state writes): {'cc','name'} or {}."""
    cc, name = "", ""
    try:
        with opener(True).open(f"http://ip-api.com/json/{ip}?fields=status,countryCode,country",
                               timeout=12) as resp:
            payload = json.loads(resp.read().decode() or "{}")
        if payload.get("status") == "success" and payload.get("countryCode"):
            cc = payload["countryCode"].upper()
            name = COUNTRY_FA.get(cc) or payload.get("country", "")
    except Exception:
        pass
    if not cc:
        try:
            with opener(True).open(f"https://ipwho.is/{ip}", timeout=12) as resp:
                payload = json.loads(resp.read().decode() or "{}")
            if payload.get("success") and payload.get("country_code"):
                cc = payload["country_code"].upper()
                name = COUNTRY_FA.get(cc) or payload.get("country", "")
        except Exception:
            pass
    if not cc:
        return {}
    return {"cc": cc, "name": name}


def geo_for_ip(ip, ttl=7 * 86400):
    """{'cc','name','flag'} for an exit IP; cached in agtool-state.json."""
    ip = (ip or "").strip()
    if not ip:
        return {}
    st = load_state()
    cache = st.get("geo") or {}
    hit = cache.get(ip) or {}
    if hit.get("cc") and time.time() - float(hit.get("ts") or 0) < ttl:
        return {"cc": hit["cc"], "name": hit.get("name") or COUNTRY_FA.get(hit["cc"], ""),
                "flag": flag_emoji(hit["cc"])}
    info = _geo_fetch(ip)
    if not info:
        return {}
    cache[ip] = {"cc": info["cc"], "name": info.get("name", ""), "ts": int(time.time())}
    st["geo"] = dict(list(cache.items())[-30:])
    save_state(st)
    return {"cc": info["cc"], "name": info.get("name", ""), "flag": flag_emoji(info["cc"])}


def prefetch_geo(ips, workers=6):
    """Resolve several IPs in parallel, then store the results in one write."""
    unique = [ip for ip in dict.fromkeys(i for i in ips if i)]
    if not unique:
        return
    cache = load_state().get("geo") or {}
    missing = [ip for ip in unique if not (cache.get(ip) or {}).get("cc")]
    if not missing:
        return
    results = {}
    try:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=min(workers, len(missing))) as pool:
            for ip, info in zip(missing, pool.map(_geo_fetch, missing)):
                if info:
                    results[ip] = info
    except Exception:
        return
    if not results:
        return
    st = load_state()
    cache = st.get("geo") or {}
    now = int(time.time())
    for ip, info in results.items():
        cache[ip] = {"cc": info["cc"], "name": info.get("name", ""), "ts": now}
    st["geo"] = dict(list(cache.items())[-30:])
    save_state(st)


def history_country(entry=None, ip=""):
    """Country label for a history row: stored first, cached lookup next."""
    entry = entry or {}
    code = entry.get("cc") or ""
    if code:
        name = entry.get("country") or COUNTRY_FA.get(code, "")
        return f"{flag_emoji(code)} {name} ({code})".strip()
    return geo_text(ip or entry.get("ip") or entry.get("to") or "")


def geo_text(ip, flags=True):
    """«🇩🇪 آلمان (DE)» or "" when the country is unknown."""
    info = geo_for_ip(ip)
    if not info:
        return ""
    label = f"{info['name']} ({info['cc']})" if info.get("name") else info["cc"]
    return f"{info['flag']} {label}" if flags and info.get("flag") else label


FLAG_DIR = os.path.join(HERE, "flags")


def flag_png(code):
    """Path to a small flag PNG (downloaded once, cached in flags\\) or ""."""
    code = (code or "").lower()
    if len(code) != 2 or not code.isalpha():
        return ""
    path = os.path.join(FLAG_DIR, f"{code}.png")
    try:
        if os.path.exists(path) and os.path.getsize(path) > 100:
            return path
    except OSError:
        return ""
    try:
        os.makedirs(FLAG_DIR, exist_ok=True)
        with opener(True).open(f"https://flagcdn.com/40x30/{code}.png", timeout=12) as resp:
            blob = resp.read()
        if len(blob) < 100 or not blob.startswith(b"\x89PNG"):
            return ""
        with open(path, "wb") as handle:
            handle.write(blob)
        return path
    except Exception:
        return ""


# ---------------------------------------------------------------- Antigravity token
def read_token():
    try:
        d = json.load(open(TOKEN_FILE))
    except Exception:
        return None
    return d.get("token") if isinstance(d.get("token"), dict) else d


def token_expiry(inner):
    raw = (inner or {}).get("expiry")
    if not raw:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            dt = datetime.datetime.strptime(raw, fmt)
            return dt.replace(tzinfo=datetime.timezone.utc) if dt.tzinfo is None else dt
        except ValueError:
            continue
    return None


def refresh_token(force=False):
    inner = read_token()
    if not inner or not inner.get("refresh_token"):
        bad("refresh_token موجود نیست — یک بار باید در IDE وارد شوی")
        return False
    exp = token_expiry(inner)
    left = (exp - datetime.datetime.now(datetime.timezone.utc)).total_seconds() if exp else -1
    if not force and left > 300:
        ok(f"توکن معتبر است ({int(left//60)} دقیقه اعتبار)")
        return True
    if not (CLIENT_ID and CLIENT_SECRET):
        bad("کلاینت OAuth تنظیم نشده — در agtool.ini بخش [antigravity] مقدار client_id و client_secret را بگذار")
        info("یا متغیر محیطی AG_CLIENT_SECRET را ست کن؛ بدون آن فقط توکن فعلی استفاده می‌شود")
        return False
    form = {"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET,
            "grant_type": "refresh_token", "refresh_token": inner["refresh_token"]}
    req = urllib.request.Request("https://oauth2.googleapis.com/token",
                                 data=urllib.parse.urlencode(form).encode())
    try:
        with opener(True).open(req, timeout=40) as r:
            t = json.loads(r.read().decode())
    except Exception as e:
        bad(f"رفرش توکن نشد: {e}")
        return False
    exp2 = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=int(t.get("expires_in", 3600)))
    out = {"token": {"access_token": t["access_token"], "token_type": t.get("token_type", "Bearer"),
                     "refresh_token": t.get("refresh_token", inner["refresh_token"]),
                     "expiry": exp2.strftime("%Y-%m-%dT%H:%M:%SZ")}}
    json.dump(out, open(TOKEN_FILE, "w"))
    try:
        os.chmod(TOKEN_FILE, 0o600)
    except OSError:
        pass
    ok(f"توکن تازه شد (تا {out['token']['expiry']} UTC)")
    return True


# ---------------------------------------------------------------- hub RPC
def find_hub():
    ps = ("Get-CimInstance Win32_Process | Where-Object {$_.Name -eq 'agy.exe' -and $_.CommandLine -match 'hub-port'} "
          "| Select-Object -First 1 -ExpandProperty CommandLine")
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                             capture_output=True, text=True, timeout=25, creationflags=NOWIN).stdout
    except Exception:
        return None, None
    m = re.search(r"hub-port=(\d+)", out or "")
    c = re.search(r"csrf_token=([0-9a-f-]+)", out or "")
    return (m.group(1) if m else None), (c.group(1) if c else None)


def hub_call(port, csrf, method, payload=None, timeout=60):
    req = urllib.request.Request(f"http://127.0.0.1:{port}/{SERVICE}/{method}",
                                 data=json.dumps(payload or {}).encode(),
                                 headers={"Content-Type": "application/json",
                                          "Connect-Protocol-Version": "1",
                                          "x-codeium-csrf-token": csrf})
    try:
        with opener(False).open(req, timeout=timeout) as r:
            return json.loads(r.read().decode() or "{}")
    except Exception:
        return {}


def account_status():
    port, csrf = find_hub()
    if not port:
        warn("سرویس زبان آنتی‌گرویتی اجرا نیست")
        return
    auth = hub_call(port, csrf, "GetAuthStatus")
    res = auth.get("authResult", {})
    if res.get("hasValidAuth"):
        ok("آنتی‌گرویتی: وارد شده (hasValidAuth=true)")
        if res.get("ineligible"):
            warn("گوگل: پرچم ineligible (محدودیت موقعیت مکانی سمت گوگل)")
    else:
        bad("آنتی‌گرویتی: وارد نشده")
    st = hub_call(port, csrf, "GetUserStatus", timeout=90)
    us = st.get("userStatus", {})
    if us.get("email"):
        plan = (us.get("planStatus", {}).get("planInfo", {}) or {}).get("planName", "?")
        credits = us.get("planStatus", {}).get("availablePromptCredits", "?")
        ok(f"حساب: {us.get('name','?')} <{us['email']}> | پلن {plan} | اعتبار {credits}")
        tiers = us.get("userTier", {})
        if tiers.get("name"):
            info(f"tier: {tiers.get('name')}")
    elif us.get("cascadeModelConfigData", {}).get("errorMessage"):
        bad(us["cascadeModelConfigData"]["errorMessage"][:120])


# ---------------------------------------------------------------- launchers
def chrome_path():
    for p in CHROME_CANDIDATES:
        if os.path.exists(p):
            return p
    return None


def open_flow():
    exe = chrome_path()
    if not exe:
        bad("chrome.exe پیدا نشد")
        return False
    subprocess.run(["taskkill", "/F", "/IM", "chrome.exe"], capture_output=True, creationflags=NOWIN)
    time.sleep(2)
    args = [exe] + chrome_proxy_args() + ["--restore-last-session"] + FLOW_URLS
    subprocess.Popen(args, creationflags=0x00000008)
    ok("کروم با پروکسی باز شد → Google Flow")
    return True


def ide_env():
    env = dict(os.environ)
    for k in ("ALL_PROXY", "all_proxy", "HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "GRPC_PROXY", "grpc_proxy"):
        env.pop(k, None)
    if PROXY:  # TUN mode: the VPN routes traffic itself, no proxy env needed
        env.update({
            "HTTPS_PROXY": PROXY, "https_proxy": PROXY, "HTTP_PROXY": PROXY, "http_proxy": PROXY,
            "ALL_PROXY": PROXY, "GRPC_PROXY": PROXY,
        })
    env.update({
        "NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost",
        "SSH_CONNECTION": "127.0.0.1 1234 127.0.0.1 22", "SSH_CLIENT": "127.0.0.1 1234 22",
    })
    return env


def launch_ide():
    if not os.path.exists(IDE_EXE):
        bad("Antigravity IDE نصب نیست")
        return False
    subprocess.run(["taskkill", "/F", "/IM", "Antigravity IDE.exe"], capture_output=True, creationflags=NOWIN)
    subprocess.run(["taskkill", "/F", "/IM", "agy.exe"], capture_output=True, creationflags=NOWIN)
    time.sleep(3)
    subprocess.Popen([IDE_EXE, f"--proxy-server={PROXY}"], env=ide_env(),
                     cwd=os.path.dirname(IDE_EXE), creationflags=0x00000008)
    ok("Antigravity با پروکسی و حالت SSH-spoofing اجرا شد")
    return True


# ---------------------------------------------------------------- commands
def cmd_status():
    print("\n── وضعیت ─────────────────────────────")
    ok("اپ MLM VPN روشن است") if mlm_running() else warn("اپ MLM VPN خاموش است (با اجرای Flow/IDE خودکار روشن می‌شود)")
    ok("پورت socks:20808 باز است") if port_open(20808) else bad("پورت socks:20808 بسته است")
    ensure_tunnel(auto_heal=False)
    ip = exit_ip()
    if ip:
        country = geo_text(ip)
        ok(f"IP خروجی تونل: {ip}" + (f"  ·  {country}" if country else ""))
    for url in ("https://labs.google/flow", "https://elevenlabs.io/"):
        code = http_status(url)
        (ok if code in (200, 302) else bad)(f"{url} → {code}")
    inner = read_token()
    exp = token_expiry(inner)
    if inner and inner.get("access_token"):
        left = (exp - datetime.datetime.now(datetime.timezone.utc)).total_seconds() if exp else -1
        (ok if left > 0 else warn)(f"توکن آنتی‌گرویتی: {'معتبر' if left > 0 else 'منقضی'} ({int(left//60)} دقیقه)")
    else:
        bad("فایل توکن آنتی‌گرویتی خالی است")
    account_status()
    print()


def cmd_flow():
    print("\n── Google Flow ───────────────────────")
    if not ensure_tunnel():
        bad("بدون تونل سالم، Flow باز نمی‌شود")
        return 1
    return 0 if open_flow() else 1


def cmd_ide():
    print("\n── Antigravity ───────────────────────")
    if not ensure_tunnel():
        bad("بدون تونل سالم، آنتی‌گرویتی کار نمی‌کند")
        return 1
    refresh_token()
    if not launch_ide():
        return 1
    for _ in range(12):
        time.sleep(5)
        port, _ = find_hub()
        if port:
            break
    account_status()
    return 0


def cmd_heal(force=False):
    print("\n── تعمیر تونل ────────────────────────")
    print(f"  {DIM}سیاست امن: اول همان نود؛ تعویض نود حداکثر هر {MIN_SWITCH_SECONDS // 60} دقیقه{END}")
    return 0 if heal_node(force=force) else 1


def cmd_preflight():
    """Read-only readiness test. Never starts the VPN, never switches a node."""
    print("\n── تست دستی قبل از باز کردن ──────────")
    st = adopt_winner()
    node = (st.get("node") or {}).get("name") or "نامشخص"
    print(f"  {DIM}نود ثبت‌شده: {node}{END}")
    problems = []
    if mlm_running():
        ok("اپ MLM VPN روشن است")
    else:
        bad("اپ MLM VPN خاموش است")
        problems.append("vpn")
    if PROXY:
        http_port, socks_port = proxy_ports()
        for port, label in ((socks_port, "socks"), (http_port, "http")):
            opened = bool(port) and port_open(port)
            (ok if opened else bad)(f"پورت {label}:{port} " + ("باز" if opened else "بسته"))
            if not opened:
                problems.append(f"port:{port}")
    else:
        info("حالت VPN سیستمی (TUN) — پورت پروکسی محلی لازم نیست")
    healthy = False
    for attempt in range(3):
        if tunnel_ok():
            healthy = True
            break
        if attempt < 2:
            info(f"تلاش {attempt + 1}/3 ناموفق — ۳ ثانیه بعد دوباره")
            time.sleep(3)
    ip = ""
    if healthy:
        ok("تونل سالم (google/generate_204 = 204)")
        ip = exit_ip()
        prev = st.get("last_ip")
        country = geo_text(ip) if ip else ""
        if ip and prev and ip != prev:
            warn(f"IP عوض شده: {prev} → {ip}" + (f"  ·  {country}" if country else "")
                 + " — کارهای حساس را کم کن، نود را دستی عوض نکن")
        elif ip:
            ok(f"IP خروجی ثابت: {ip}" + (f"  ·  {country}" if country else ""))
        remember_ip(ip)
    else:
        bad("تونل جواب نداد (هر ۳ تلاش)")
        problems.append("tunnel")
    for url in ("https://labs.google/flow", "https://accounts.google.com/"):
        code = http_status(url)
        (ok if code in (200, 302) else bad)(f"{url} → {code}")
        if code not in (200, 302):
            problems.append(url)
    inner = read_token()
    exp = token_expiry(inner)
    left = (exp - datetime.datetime.now(datetime.timezone.utc)).total_seconds() if exp else -1
    if left > 300:
        ok(f"توکن آنتی‌گرویتی معتبر ({int(left // 60)} دقیقه)")
    elif inner:
        warn("توکن نزدیک انقضا — با اجرای Antigravity خودکار تازه می‌شود")
    else:
        bad("فایل توکن آنتی‌گرویتی موجود نیست")
        problems.append("token")
    hub_port, _ = find_hub()
    if hub_port:
        account_status()
    else:
        info("سرویس زبان اجرا نیست (IDE بسته است) — بررسی حساب انجام نشد")
    cd = switch_cooldown_left()
    info(f"قفل تعویض نود: {'فعال، ' + str(int(cd // 60)) + ' دقیقه مانده' if cd else 'آزاد'}")
    print()
    if problems:
        print(f"  {RED}⛔ آماده نیست{END} — مشکل: " + ", ".join(problems))
        print(f"  {DIM}تست هیچ تغییری ایجاد نکرد. برای رفع: python agtool.py heal{END}")
        return 1
    print(f"  {GREEN}✅ آماده است{END} — Flow/IDE را باز کن و نود را دستی عوض نکن")
    return 0

def fmt_ts(ts):
    try:
        return datetime.datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return "?"


def age_str(ts):
    try:
        secs = max(0, int(time.time() - float(ts)))
    except Exception:
        return "?"
    if secs < 3600:
        return f"{secs // 60} دقیقه پیش"
    if secs < 86400:
        return f"{secs // 3600} ساعت و {(secs % 3600) // 60} دقیقه پیش"
    return f"{secs // 86400} روز پیش"


def _entry_country(ip, entry=None):
    """(cc, country name) for a history row: stored value first, cached geo next."""
    entry = entry or {}
    code = str(entry.get("cc") or "").upper()
    if code:
        return code, entry.get("country") or COUNTRY_FA.get(code, "")
    if ip:
        info = geo_for_ip(ip) or {}
        return info.get("cc", ""), info.get("name", "")
    return "", ""


def history_summary(hours=24):
    """Which countries the exit IP went through inside the last `hours` hours."""
    st = load_state()
    limit = time.time() - hours * 3600
    switches = [s for s in (st.get("switches") or []) if float(s.get("ts") or 0) >= limit]
    ips = [h for h in (st.get("ip_history") or []) if float(h.get("ts") or 0) >= limit]
    countries = {}

    def add(code, name):
        if not code:
            return
        row = countries.setdefault(code, [0, name or COUNTRY_FA.get(code, "")])
        row[0] += 1

    for switch in switches:
        add(*_entry_country(switch.get("ip"), switch))
    for change in ips:
        add(*_entry_country(change.get("to"), change))
        add(*_entry_country(change.get("from"), change))
    last_ip = st.get("last_ip")
    if last_ip:
        add(*_entry_country(last_ip))
    ordered = sorted(countries.items(), key=lambda item: (-item[1][0], item[0]))
    return {"countries": ordered, "switches": len(switches), "ip_changes": len(ips),
            "last_ip": last_ip or ""}


def cmd_history():
    """Read-only report: how often the exit IP / node actually changed."""
    print("\n── تاریخچهٔ IP و تعویض نود ────────────")
    st = load_state()
    adopt_winner()
    st = load_state()
    prefetch_geo([st.get("last_ip")] + [s.get("ip") for s in (st.get("switches") or [])]
                 + [h.get("to") for h in (st.get("ip_history") or [])]
                 + [h.get("from") for h in (st.get("ip_history") or [])])
    summary = history_summary()
    if summary["countries"]:
        labels = " · ".join(f"{flag_emoji(code)} {name or code} ({code})"
                            for code, (_count, name) in summary["countries"])
        print(f"  خلاصهٔ ۲۴ ساعت گذشته: {len(summary['countries'])} کشور — {labels}")
    else:
        warn("خلاصهٔ ۲۴ ساعت گذشته: هیچ IP ثبت‌شده‌ای در این بازه نیست")
    print(f"  در ۲۴ ساعت: {summary['ip_changes']} تغییر IP · {summary['switches']} تعویض نود")
    print()
    node = st.get("node") or {}
    if node.get("name"):
        since = f"  (از {fmt_ts(node['switched_at'])})" if node.get("switched_at") else ""
        print(f"  نود فعلی: {node['name']}{since}")
    else:
        warn("نود فعلی ثبت نشده")
    last_ip = st.get("last_ip")
    if last_ip:
        when = f" | آخرین ثبت: {age_str(st.get('last_ip_ts'))}" if st.get("last_ip_ts") else ""
        country = geo_text(last_ip)
        print(f"  IP آخر: {last_ip}" + (f"  ·  {country}" if country else "") + when)
    switches = st.get("switches") or []
    ips = st.get("ip_history") or []
    total_sw = int(st.get("switch_count") or len(switches))
    day_sw = len([s for s in switches if time.time() - float(s.get("ts") or 0) < 86400])
    print(f"  تعویض نود: {total_sw} بار در کل | {day_sw} بار در ۲۴ ساعت گذشته")
    print(f"  تغییر IP: {len(ips)} بار ثبت شده")
    if switches:
        print(f"\n  {DIM}تعویض‌های اخیر:{END}")
        for s in switches[-10:]:
            country = history_country(s)
            print(f"    {fmt_ts(s.get('ts'))}  {s.get('name','?'):<10}  IP: {s.get('ip') or '?':<15}"
                  + (f"  {country}" if country else ""))
    if ips:
        print(f"\n  {DIM}تغییرات IP:{END}")
        for h in ips[-10:]:
            country = history_country(h, h.get("to") or "")
            print(f"    {fmt_ts(h.get('ts'))}  {h.get('from')} → {h.get('to')}"
                  + (f"  ·  {country}" if country else ""))
    print()
    if total_sw == 0 and not ips:
        ok("هیچ تعویض نود یا تغییر IP ثبت نشده — وضعیت پایدار و کم‌ریسک")
    elif total_sw <= 2 and len(ips) <= 2:
        ok("تغییرات خیلی کم بوده — ریسک پایین")
    else:
        warn("تغییر IP زیاد بوده — کمتر نود عوض کن (ریسک بن اکانت)")
    return 0


def watch_interval_default():
    try:
        return max(10, int(cfg("watch", "interval_seconds", "120")))
    except ValueError:
        return 120


def watchdog_pid():
    if FROZEN:
        exe = os.path.basename(sys.executable).replace("'", "''")
        ps = (f"Get-CimInstance Win32_Process | Where-Object {{ $_.Name -eq '{exe}' -and "
              "$_.CommandLine -like '*watch*' } | Select-Object -First 1 -ExpandProperty ProcessId")
    else:
        ps = ("Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'pythonw.exe' -and "
              "$_.CommandLine -like '*agtool.py*' } | Select-Object -First 1 -ExpandProperty ProcessId")
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                             capture_output=True, text=True, timeout=25, creationflags=NOWIN).stdout.strip()
        return int(out) if out.isdigit() else None
    except Exception:
        return None


def start_watchdog(interval=None):
    pyw = find_pythonw()
    if not pyw:
        return None
    interval = int(interval or watch_interval_default())
    args = ([pyw, "watch", str(interval)] if FROZEN
            else [pyw, os.path.join(HERE, "agtool.py"), "watch", str(interval)])
    p = subprocess.Popen(args, creationflags=NOWIN)
    return p.pid


def stop_watchdog():
    pid = watchdog_pid()
    if not pid:
        return False
    subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True, creationflags=NOWIN)
    return True


def collect_status(tries=3):
    """Structured health snapshot (used by the GUI and the `json` command)."""
    checks = []

    def add(name, state, detail):
        checks.append({"name": name, "state": state, "detail": detail})

    st = adopt_winner()
    node = (st.get("node") or {}).get("name") or "نامشخص"
    running = mlm_running()
    add("اپ MLM VPN", "ok" if running else "bad", "روشن است" if running else "خاموش است")
    http_port, socks_port = proxy_ports()
    if PROXY:
        p_http = bool(http_port) and port_open(http_port)
        p_socks = bool(socks_port) and port_open(socks_port)
        add("پورت‌های پروکسی", "ok" if (p_socks and p_http) else "bad",
            f"{socks_port} و {http_port} باز" if (p_socks and p_http)
            else f"socks {socks_port}={int(p_socks)} · http {http_port}={int(p_http)}")
    else:
        add("حالت اتصال", "ok", "VPN سیستمی (TUN) — بدون پروکسی محلی")
    healthy = False
    for i in range(max(1, tries)):
        if tunnel_ok():
            healthy = True
            break
        if i < tries - 1:
            time.sleep(2)
    ip = exit_ip() if healthy else ""
    add("تونل پروکسی", "ok" if healthy else "bad",
        "۲۰۴ سالم" if healthy else "پاسخ نداد (۳ تلاش)")
    geo = geo_for_ip(ip) if healthy and ip else {}
    if healthy:
        prev = st.get("last_ip")
        changed = bool(prev and ip and prev != ip)
        country = f"{geo['name']} ({geo['cc']})" if geo else ""
        add("IP خروجی", "warn" if changed else "ok",
            (f"{country} · {ip}" if country else ip)
            + (f" (قبلی {prev})" if changed else " — ثابت"))
        remember_ip(ip)
    code_flow = http_status("https://labs.google/flow")
    code_google = http_status("https://accounts.google.com/")
    google_ok = code_flow in (200, 302) and code_google in (200, 302)
    add("گوگل (Flow + ورود)", "ok" if google_ok else "bad",
        f"Flow {code_flow} · accounts {code_google}")
    inner = read_token()
    exp = token_expiry(inner)
    left = (exp - datetime.datetime.now(datetime.timezone.utc)).total_seconds() if exp else -1
    add("توکن Antigravity", "ok" if left > 300 else ("warn" if inner else "bad"),
        f"{int(left // 60)} دقیقه اعتبار" if left > 0 else ("نیاز به تازه‌سازی" if inner else "موجود نیست"))
    hub_port, csrf = find_hub()
    if hub_port:
        auth = hub_call(hub_port, csrf, "GetAuthStatus", timeout=30).get("authResult", {})
        add("ورود Antigravity", "ok" if auth.get("hasValidAuth") else "bad",
            "وارد شده" if auth.get("hasValidAuth") else "وارد نشده")
        us = hub_call(hub_port, csrf, "GetUserStatus", timeout=40).get("userStatus", {})
        if us.get("email"):
            plan = (us.get("planStatus", {}).get("planInfo", {}) or {}).get("planName", "?")
            credits = us.get("planStatus", {}).get("availablePromptCredits", "?")
            add("حساب", "ok", f"{us['email']} · پلن {plan} · اعتبار {credits}")
    else:
        add("ورود Antigravity", "warn", "IDE بسته است — بررسی نشد")
    cd = switch_cooldown_left()
    add("قفل تعویض نود", "warn" if cd else "ok",
        f"{int(cd // 60)} دقیقهٔ دیگر" if cd else "آزاد (بدون ریسک تغییر IP)")
    add("نظارت خودکار", "ok" if watchdog_pid() else "warn",
        "در حال اجراست" if watchdog_pid() else "خاموش")
    ok_all = not any(c["state"] == "bad" for c in checks)
    summary = history_summary()
    return {"checks": checks, "node": node, "ip": ip, "ok": ok_all,
            "geo": geo, "node_cc": node_country_code(node),
            "time": datetime.datetime.now().strftime("%H:%M:%S"),
            "switch_count": int(load_state().get("switch_count") or 0),
            "countries": [{"cc": code, "name": name, "count": count}
                          for code, (count, name) in summary["countries"]],
            "ips24": summary["ip_changes"], "switches24": summary["switches"]}


def cmd_json():
    print(json.dumps(collect_status(), ensure_ascii=False))
    return 0


# ------------------------------------------------------------ VPN presets
VPN_PRESETS = [
    {"id": "mlm", "name": "MLM VPN", "http": "http://127.0.0.1:20809",
     "socks": "socks5h://127.0.0.1:20808", "api": "http://127.0.0.1:62569",
     "app": r"C:\Program Files\MLM VPN\MLM VPN.exe"},
    {"id": "v2rayn", "name": "v2rayN", "http": "http://127.0.0.1:10809",
     "socks": "socks5h://127.0.0.1:10808", "api": "", "app": ""},
    {"id": "clash", "name": "Clash / Mihomo", "http": "http://127.0.0.1:7890",
     "socks": "socks5h://127.0.0.1:7891", "api": "", "app": ""},
    {"id": "hiddify", "name": "Hiddify", "http": "http://127.0.0.1:10801",
     "socks": "socks5h://127.0.0.1:10801", "api": "", "app": ""},
    {"id": "nekobox", "name": "NekoBox / NekoRay", "http": "http://127.0.0.1:2080",
     "socks": "socks5h://127.0.0.1:2081", "api": "", "app": ""},
    {"id": "tun", "name": "OpenVPN / WireGuard (TUN)", "http": "", "socks": "",
     "api": "", "app": "", "mode": "direct"},
]


def preset_by_id(preset_id):
    wanted = str(preset_id or "").lower()
    for preset in all_presets():
        if preset["id"].lower() == wanted:
            return preset
    return None


def preset_port(preset):
    try:
        return urllib.parse.urlparse(preset.get("http") or "").port
    except Exception:
        return None


def preset_ports(preset):
    """(http_port, socks_port) from the preset's default URLs."""
    def _port(url):
        try:
            return urllib.parse.urlparse(url or "").port
        except Exception:
            return None

    return _port(preset.get("http")), _port(preset.get("socks"))


def parse_port(value, label="پورت"):
    try:
        port = int(str(value).strip())
    except (TypeError, ValueError):
        raise ValueError(f"{label} باید عدد باشد — مقدار داده‌شده: {value}")
    if not 1 <= port <= 65535:
        raise ValueError(f"{label} باید بین ۱ و ۶۵۵۳۵ باشد — مقدار داده‌شده: {port}")
    return port


def with_port(url, port, label="پورت"):
    """Same URL with a different port (scheme/host/path unchanged)."""
    port = parse_port(port, label)
    parts = urllib.parse.urlparse(url or "")
    if not parts.hostname:
        raise ValueError(f"آدرس پروکسی نامعتبر است: {url}")
    return parts._replace(netloc=f"{parts.hostname}:{port}").geturl()


def url_port(url):
    """Port of a proxy URL, or None when it has no usable port."""
    try:
        return urllib.parse.urlparse(url or "").port
    except ValueError:
        return None


def remember_vpn(preset_id):
    """Remember which preset was applied (custom ports make URL matching ambiguous)."""
    st = load_state()
    st["vpn"] = preset_id
    save_state(st)


def current_vpn_id():
    """The selected preset: the remembered one if it still fits, else an exact URL match."""
    ids = {preset["id"] for preset in VPN_PRESETS}
    remembered = str(load_state().get("vpn") or "")
    if remembered in ids:
        preset = preset_by_id(remembered)
        if preset.get("mode") == "direct":
            if not PROXY:
                return remembered
        else:
            now = urllib.parse.urlparse(PROXY or "")
            default = urllib.parse.urlparse(preset.get("http") or "")
            if now.scheme == default.scheme and now.hostname == default.hostname:
                return remembered
    for preset in VPN_PRESETS:
        if (preset.get("http") or "") == PROXY and (preset.get("socks") or "") == SOCKS:
            return preset["id"]
    return ""


def preset_detected(preset):
    """Is this client listening right now? None = cannot be detected automatically (TUN)."""
    if preset.get("mode") == "direct":
        return None
    port = preset_port(preset)
    return bool(port and port_open(port))


def write_ini_values(section, values):
    """Update keys of one section in agtool.ini, keeping comments and order."""
    try:
        with open(CONFIG_FILE, encoding="utf-8") as handle:
            lines = handle.read().splitlines()
    except OSError:
        return False
    out, current, seen = [], None, set()
    for line in lines:
        header = re.match(r"^\s*\[([^\]]+)\]", line)
        if header:
            if current == section:
                out.extend(f"{k} = {v}" for k, v in values.items() if k not in seen)
            current = header.group(1).strip()
        elif current == section:
            key = re.match(r"^(\s*)([A-Za-z_][A-Za-z0-9_]*)\s*=", line)
            if key and key.group(2) in values:
                line = f"{key.group(2)} = {values[key.group(2)]}"
                seen.add(key.group(2))
        out.append(line)
    if current == section:
        out.extend(f"{k} = {v}" for k, v in values.items() if k not in seen)
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8", newline="\n") as handle:
            handle.write("\n".join(out) + "\n")
    except OSError:
        return False
    return True


def _upgrade_config_file():
    """A newer app version may add sections/keys: append them with default values."""
    global _CFG
    if not os.path.exists(CONFIG_FILE):
        return
    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read(CONFIG_FILE, encoding="utf-8")
    except Exception:
        return
    defaults = configparser.ConfigParser(interpolation=None)
    defaults.read_string(_DEFAULT_CONFIG)
    changed = False
    for section in defaults.sections():
        if parser.has_section(section):
            continue
        try:
            with open(CONFIG_FILE, "a", encoding="utf-8", newline="\n") as handle:
                handle.write(f"\n[{section}]\n")
                for key, value in defaults.items(section):
                    handle.write(f"{key} = {value}\n")
            changed = True
        except OSError:
            continue
    for section in defaults.sections():
        if not parser.has_section(section):
            continue
        missing = {key: defaults.get(section, key) for key in defaults.options(section)
                   if not parser.has_option(section, key)}
        if missing and write_ini_values(section, missing):
            changed = True
    if changed:
        _CFG = _load_config(CONFIG_FILE)


_upgrade_config_file()


def apply_vpn(preset_id, http_port=None, socks_port=None):
    """Write the chosen preset into agtool.ini.

    Empty/None ports keep the preset's own default port; otherwise the given
    custom port replaces it (for users who changed the client's ports).
    """
    preset = preset_by_id(preset_id)
    if not preset:
        return False
    http = preset.get("http", "")
    socks = preset.get("socks", "")
    try:
        try:
            http = with_port(http, http_port, "پورت HTTP") if str(http_port or "").strip() else http
        except ValueError:
            raise ValueError(f"پورت HTTP نامعتبر است: {http_port}")
        try:
            socks = with_port(socks, socks_port, "پورت SOCKS") if str(socks_port or "").strip() else socks
        except ValueError:
            raise ValueError(f"پورت SOCKS نامعتبر است: {socks_port}")
    except ValueError as exc:
        if preset.get("mode") == "direct":
            bad("این پریست پروکسی ندارد (VPN سیستمی/TUN) — پورت سفارشی لازم نیست")
        else:
            bad(str(exc))
        return False
    if not write_ini_values("vpn", {
        "http_proxy": http,
        "socks_proxy": socks,
        "api": preset.get("api", ""),
        "app_exe": preset.get("app", ""),
    }):
        return False
    remember_vpn(preset["id"])
    return True


def cmd_vpn(target="", http_port="", socks_port=""):
    print("\n── فیلترشکن ─────────────────────────")
    target = (target or "").lower()
    if target in ("", "list"):
        current = current_vpn_id()
        if PROXY or SOCKS:
            info(f"در حال استفاده: {PROXY or '—'} · {SOCKS or '—'}")
        for preset in all_presets():
            live = preset_detected(preset)
            mark = "*" if preset["id"] == current else ("+" if live else ("?" if live is None else "-"))
            where = preset.get("http") or "بدون پروکسی (TUN)"
            state = "در حال اجرا" if live else ("تشخیص خودکار ندارد" if live is None else "پیدا نشد")
            print(f"  [{mark}] {preset['name']:<24} {where:<28} {state}")
        print(f"  {DIM}* = انتخاب فعلی   + = پیدا شد   - = پیدا نشد   ? = خودکار تشخیص داده نمی‌شود{END}")
        print(f"  {DIM}انتخاب: python agtool.py vpn apply <id> [پورت HTTP] [پورت SOCKS]  ·  خودکار: vpn auto{END}")
        return 0
    if target == "auto":
        for preset in all_presets():
            if preset.get("mode") != "direct" and preset_detected(preset):
                info(f"تشخیص خودکار: {preset['name']} روی {preset.get('http')}")
                target = preset["id"]
                break
        else:
            bad("هیچ فیلترشکن شناخته‌شده‌ای روی پورت پیش‌فرضش پیدا نشد")
            info("اگر فیلترشکن شما TUN است: python agtool.py vpn apply tun")
            return 1
    preset = preset_by_id(target)
    if not preset:
        bad(f"پریست ناشناخته: {target}")
        return 1
    if not apply_vpn(target, http_port, socks_port):
        bad("نوشتن agtool.ini ممکن نشد")
        return 1
    ok(f"«{preset['name']}» انتخاب شد و در agtool.ini ذخیره شد")
    port_parts = []
    if str(http_port or "").strip():
        port_parts.append(f"HTTP={http_port}")
    if str(socks_port or "").strip():
        port_parts.append(f"SOCKS={socks_port}")
    if port_parts:
        info("پورت سفارشی: " + "  ".join(port_parts))
    else:
        info(f"پورت‌های پیش‌فرض پریست: HTTP={preset.get('http') or '—'} · SOCKS={preset.get('socks') or '—'}")
    info("برنامه را کامل ببندید و دوباره باز کنید تا اعمال شود")
    return 0


def cmd_config(show_file=False):
    print("\n── تنظیمات ──────────────────────────")
    info(CONFIG_FILE)
    rows = [
        ("پروکسی HTTP", PROXY),
        ("فاصلهٔ نظارت", f"{watch_interval_default()} ثانیه"),
        ("پروکسی SOCKS", SOCKS),
        ("API اپ VPN", MLM_API or "— (تنظیم نشده)"),
        ("اپ VPN", MLM_EXE or "— (تنظیم نشده)"),
        ("Antigravity", IDE_EXE),
        ("قفل تعویض نود", f"{MIN_SWITCH_SECONDS // 60} دقیقه"),
        ("اعلان ویندوز", "روشن" if NOTIFY_ENABLED else "خاموش"),
    ]
    for label, value in rows:
        print(f"  {label:<16} {value}")
    problems = validate_config()
    if problems:
        print()
        for level, message in problems:
            (bad if level == "error" else warn)(message)
    else:
        ok("همهٔ مقدارهای تنظیمات معتبرند")
    if show_file:
        try:
            os.startfile(CONFIG_FILE)
            ok("فایل تنظیمات باز شد")
        except OSError as exc:
            bad(f"باز کردن فایل ممکن نشد: {exc}")
    return 0


def single_instance(name=MUTEX_NAME):
    """True if this process is the only watchdog. Prevents duplicate autostart runs."""
    try:
        ctypes.windll.kernel32.CreateMutexW(None, False, name)
        return ctypes.windll.kernel32.GetLastError() != 183  # ERROR_ALREADY_EXISTS
    except Exception:
        return True


def find_pythonw():
    if FROZEN:
        return sys.executable  # the watchdog is the same exe with the "watch" argument
    cands = [os.path.join(sys.base_prefix, "pythonw.exe"),
             os.path.join(os.path.dirname(sys.executable), "pythonw.exe")]
    for c in cands:
        if os.path.exists(c):
            return c
    return None


def cmd_autostart(mode="status", interval=120):
    """Create/remove the silent (pythonw, no console) watchdog entry in the Startup folder."""
    mode = (mode or "status").lower()
    if mode == "off":
        if os.path.exists(STARTUP_LNK):
            os.remove(STARTUP_LNK)
            ok("استارت خودکار خاموش شد (فایل Startup حذف شد)")
        else:
            info("استارت خودکار از قبل خاموش بود")
        return 0
    if mode == "status":
        if os.path.exists(STARTUP_LNK):
            ok(f"استارت خودکار فعال است → {STARTUP_LNK}")
        else:
            warn("استارت خودکار خاموش است")
        return 0
    pyw = find_pythonw()
    if not pyw:
        bad("pythonw.exe پیدا نشد")
        return 1
    if not os.path.isdir(STARTUP_DIR):
        bad(f"پوشهٔ Startup پیدا نشد: {STARTUP_DIR}")
        return 1
    lnk = STARTUP_LNK.replace("'", "''")
    arguments = "watch" if FROZEN else f'"{os.path.join(HERE, "agtool.py")}" watch'
    script = (
        "$w = New-Object -ComObject WScript.Shell; "
        f"$s = $w.CreateShortcut('{lnk}'); "
        f"$s.TargetPath = '{pyw}'; "
        f"$s.Arguments = '{arguments}'; "
        f"$s.WorkingDirectory = '{HERE}'; "
        "$s.WindowStyle = 7; "
        "$s.Description = 'AI Tool watchdog: silent tunnel monitor'; "
        "$s.Save()"
    )
    r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                       capture_output=True, text=True, timeout=60, creationflags=NOWIN)
    if os.path.exists(STARTUP_LNK):
        ok(f"استارت خودکار روشن شد (بی‌صدا · فاصله از agtool.ini: {watch_interval_default()} ثانیه)")
        info(STARTUP_LNK)
        return 0
    bad(f"ساخت شورتکات Startup ناموفق: {(r.stderr or '')[:150]}")
    return 1


def log_line(msg):
    line = f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        if os.path.exists(WATCH_LOG) and os.path.getsize(WATCH_LOG) > 1_000_000:
            os.replace(WATCH_LOG, WATCH_LOG + ".old")
        with open(WATCH_LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def cmd_watch(interval=0, fails_needed=3):
    """Keep the tunnel alive: check every `interval` seconds, switch node after
    `fails_needed` consecutive failures. Stops on Ctrl+C."""
    interval = max(10, int(interval or watch_interval_default()))
    if not single_instance():
        print("  نظارت از قبل در حال اجراست (نسخهٔ دوم اجرا نمی‌شود)")
        log_line("second watchdog instance refused (already running)")
        return 0
    print(f"\n── نظارت بر تونل (هر {interval} ثانیه) ──────")
    print(f"  {DIM}لاگ: {WATCH_LOG}{END}")
    print(f"  {DIM}برای توقف: Ctrl+C{END}\n")
    log_line(f"watchdog start | interval={interval}s | threshold={fails_needed}")
    time.sleep(15)  # grace period: let the VPN/proxy settle after login
    failures = 0
    try:
        while True:
            if not mlm_running():
                log_line("اپ MLM VPN خاموش است → روشن کردن")
                start_mlm()
            if tunnel_ok():
                log_line("تونل برگشت → سالم" if failures else "تونل سالم")
                failures = 0
                try:
                    quota = check_quota()
                    if quota and quota.get("credits") is not None:
                        log_line(f"اعتبار آنتی‌گرویتی: {quota.get('credits')}")
                except Exception:
                    pass
            else:
                failures += 1
                log_line(f"تونل جواب نداد ({failures}/{fails_needed})")
                if failures >= fails_needed:
                    log_line("تونل مرده → بازگردانی با کمترین تغییر IP ...")
                    notify("AI Tool — تونل قطع شد", f"{fails_needed} تست پشت سر هم ناموفق؛ تلاش برای بازگردانی")
                    if heal_node():
                        log_line("تونل برگشت (IP تا حد امکان ثابت)")
                        notify("AI Tool — تونل برگشت", "تونل پروکسی سالم شد")
                        failures = 0
                    else:
                        log_line("برنگشت یا قفل تعویض نود فعال است → تلاش دوباره")
                        time.sleep(interval * 2)
            time.sleep(interval)
    except KeyboardInterrupt:
        log_line("watchdog stopped by user")
        return 0


# ============================================================== extras
# Portable profile, node latency test, update check, quota watch,
# diagnostic bundle, proxy env vars, PIN lock, custom VPN detection and
# the safe parts of the Antigravity-cleaner toolchain (doctor/clean/kill).
APP_VERSION = cfg("update", "version", "1.1.0")
PRESETS_FILE = os.path.join(HERE, "agtool-presets.json")
PROFILE_MAGIC = "aitool-profile-1"
PIN_SALT = "AI-Tool::pin::v1"
DESKTOP = os.path.join(os.path.expanduser("~"), "Desktop")
APP_CACHE_DIRS = ["Cache", "Code Cache", "GPUCache", "DawnCache", "DawnGraphiteCache",
                  "DawnWebGPUCache", "CachedData", "CachedProfilesData", "ShaderCache",
                  "GrShaderCache", "logs"]


def load_custom_presets():
    """VPN presets added by the user (any client the built-in list does not know)."""
    try:
        data = json.load(open(PRESETS_FILE, encoding="utf-8"))
    except Exception:
        return []
    return [p for p in data if isinstance(p, dict) and p.get("id")] if isinstance(data, list) else []


def save_custom_presets(items):
    try:
        with open(PRESETS_FILE, "w", encoding="utf-8") as handle:
            json.dump(items, handle, ensure_ascii=False, indent=1)
        return True
    except OSError:
        return False


def all_presets():
    return VPN_PRESETS + load_custom_presets()


# ---------------------------------------------------------------- profile
def _ini_text():
    try:
        with open(CONFIG_FILE, encoding="utf-8") as handle:
            return handle.read()
    except OSError:
        return _DEFAULT_CONFIG


def _strip_pin(text):
    """A shared profile must never carry somebody else's settings lock."""
    out = []
    for line in (text or "").splitlines():
        out.append("pin_hash =" if re.match(r"^\s*pin_hash\s*=", line) else line)
    return "\n".join(out) + "\n"


def merge_nodes(nodes):
    """Add nodes from a profile to the local node list (no duplicates by uri)."""
    current = load_nodes()
    seen = {n.get("uri") for n in current}
    added = [n for n in nodes if n.get("uri") and n["uri"] not in seen]
    if not added:
        return 0
    try:
        with open(os.path.join(HERE, "mlm_found_nodes.json"), "w", encoding="utf-8") as handle:
            json.dump(current + added, handle, ensure_ascii=False, indent=1)
    except OSError:
        return 0
    return len(added)


def export_profile(path="", include_identity=True):
    """.aitool-profile: settings + presets + node list in one portable file.

    include_identity=False leaves out the current node and exit IP, so the
    receiving machine starts with its own node (no shared exit IP).
    """
    st = load_state()
    state = {k: st.get(k) for k in ("node", "last_ip", "last_ip_ts", "vpn") if st.get(k)}
    if not include_identity:
        state = {k: v for k, v in state.items() if k == "vpn"}
    payload = {
        "magic": PROFILE_MAGIC,
        "app": "AI Tool",
        "version": APP_VERSION,
        "created": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "ini": _ini_text(),
        "nodes": load_nodes(),
        "custom_presets": load_custom_presets(),
        "state": state,
    }
    path = path or os.path.join(HERE, f"AI-Tool-{datetime.datetime.now():%Y%m%d-%H%M}.aitool-profile")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=1)
    return path


def import_profile(path, apply_state=True):
    """Restore a profile. Invalid files are rejected before anything is written."""
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    if data.get("magic") != PROFILE_MAGIC:
        raise ValueError("این فایل پروفایل AI Tool نیست")
    ini_text = data.get("ini") or ""
    if "[vpn]" not in ini_text:
        raise ValueError("بخش [vpn] در این پروفایل نیست")
    incoming = CONFIG_FILE + ".incoming"
    with open(incoming, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(_strip_pin(ini_text))
    problems = validate_config(incoming)
    errors = [msg for level, msg in problems if level == "error"]
    if errors:
        os.remove(incoming)
        raise ValueError("پروفایل مقدار نامعتبر دارد: " + " | ".join(errors))
    if os.path.exists(CONFIG_FILE):
        try:
            shutil.copy2(CONFIG_FILE, CONFIG_FILE + ".bak")
        except OSError:
            pass
    os.replace(incoming, CONFIG_FILE)
    added = merge_nodes(data.get("nodes") or [])
    presets = data.get("custom_presets") or []
    if presets:
        merged = {p["id"]: p for p in load_custom_presets()}
        merged.update({p["id"]: p for p in presets if p.get("id")})
        save_custom_presets(list(merged.values()))
    if apply_state:
        st = load_state()
        incoming = data.get("state") or {}
        for key in ("node", "last_ip", "last_ip_ts", "vpn"):
            if key in incoming:
                st[key] = incoming[key]
            elif key in ("node", "last_ip", "last_ip_ts"):
                # identity-free profile: forget the previous node/IP of this machine
                st.pop(key, None)
        save_state(st)
    return {"nodes": added, "presets": len(presets),
            "warnings": [m for lvl, m in problems if lvl == "warn"]}


def cmd_profile(action="export", path="", fresh=False):
    print("\n── پروفایل قابل انتقال ───────────────")
    action = (action or "export").lower()
    if action in ("export", "export-fresh"):
        if action == "export-fresh":
            fresh = True
        try:
            saved = export_profile(path, include_identity=not fresh)
        except Exception as exc:
            bad(f"ذخیرهٔ پروفایل ناموفق: {exc}")
            return 1
        ok(f"پروفایل ساخته شد: {saved}")
        if fresh:
            info("بدون نود و بدون IP — مقصد با نود خودش شروع می‌کند")
        info(f"حجم: {os.path.getsize(saved) // 1024} کیلوبایت — همین یک فایل را برای دوستت بفرست")
        info("شامل: تنظیمات، پریست‌های فیلترشکن، فهرست نود و انتخاب فعلی (بدون هیچ توکنی)")
        return 0
    if action == "import":
        if not path:
            bad("مسیر فایل پروفایل را بده: profile import <file.aitool-profile>")
            return 1
        try:
            result = import_profile(path)
        except Exception as exc:
            bad(str(exc))
            return 1
        ok("پروفایل بازیابی شد (نسخهٔ قبلی agtool.ini به agtool.ini.bak ذخیره شد)")
        info(f"نود تازه: {result['nodes']} · پریست: {result['presets']}")
        for msg in result["warnings"]:
            warn(msg)
        info("برنامه را کامل ببند و دوباره باز کن تا تنظیمات تازه اعمال شود")
        return 0
    bad(f"کار نامشخص: {action} — export یا import")
    return 1


# ---------------------------------------------------------------- node latency
def node_host_port(node):
    uri = node.get("uri") or ""
    try:
        tail = uri.split("@")[-1].split("/")[0].split("?")[0]
        host, _, port = tail.rpartition(":")
        return host.strip("[]"), int(port)
    except Exception:
        return "", 0


def ping_node(node, tries=2, timeout=2.5):
    """TCP handshake latency to the node's own address. Never connects the client."""
    host, port = node_host_port(node)
    if not host or not port:
        return None
    best = None
    for _ in range(tries):
        sock = socket.socket()
        sock.settimeout(timeout)
        start = time.perf_counter()
        started = sock.connect_ex((host, port)) == 0
        elapsed = (time.perf_counter() - start) * 1000
        sock.close()
        if started:
            best = elapsed if best is None else min(best, elapsed)
    return best


def cmd_nodes(limit=0, save=True, workers=8):
    print("\n── تست سرعت نودها (بدون اتصال) ───────")
    nodes = load_nodes()
    if not nodes:
        bad("فهرست نود خالی است (mlm_found_nodes.json)")
        return 1
    if limit:
        nodes = nodes[:int(limit)]
    info(f"{len(nodes)} نود — فقط دست‌دادن TCP با آدرس نود سنجیده می‌شود؛ هیچ نودی وصل نمی‌شود و IP عوض نمی‌شود")
    report_progress(5, "شروع تست نودها")
    results = []
    try:
        from concurrent.futures import ThreadPoolExecutor, as_completed
        total = max(1, len(nodes))
        done = 0
        with ThreadPoolExecutor(max_workers=max(1, min(workers, len(nodes)))) as pool:
            futures = {pool.submit(ping_node, node): node for node in nodes}
            for future in as_completed(futures):
                node = futures[future]
                try:
                    delay = future.result()
                except Exception:
                    delay = None
                results.append((delay, node))
                done += 1
                report_progress(5 + int(90 * done / total), f"تست نود {done}/{total}")
    except Exception:
        total = max(1, len(nodes))
        for index, node in enumerate(nodes):
            results.append((ping_node(node), node))
            report_progress(5 + int(90 * (index + 1) / total), f"تست نود {index + 1}/{total}")
    report_progress(96, "جمع‌بندی نتیجه‌ها")
    results.sort(key=lambda item: item[0] if item[0] is not None else 99999)
    reachable = 0
    for delay, node in results:
        name = node_name(node) or node.get("id", "?")
        cc = node_country_code(name) or node.get("cc", "")
        country = COUNTRY_FA.get(cc, "") if cc else ""
        label = f"{flag_emoji(cc)} {country} ({cc})".strip() if cc else "—"
        if delay is None:
            print(f"    {name:<16} {label:<20} {RED}بی‌پاسخ{END}")
        else:
            reachable += 1
            print(f"    {name:<16} {label:<20} {int(delay)} میلی‌ثانیه")
    best = next((item for item in results if item[0] is not None), None)
    print()
    if best:
        ok(f"سریع‌ترین: {node_name(best[1]) or '?'} با {int(best[0])} میلی‌ثانیه (از {reachable} نود پاسخ‌ده)")
    else:
        bad("هیچ نودی به دست‌دادن TCP پاسخ نداد (اینترنت/NAT را ببین)")
    report_progress(100, "تست تمام شد")
    warn("این تست انتخاب خودکار نود نیست؛ اگر نود عوض کنی IP عوض می‌شود (ریسک بن)")
    if save:
        st = load_state()
        st["node_latency"] = {"ts": int(time.time()),
                              "rows": [{"name": node_name(n) or n.get("id", ""),
                                        "ms": None if d is None else int(d)} for d, n in results[:40]]}
        save_state(st)
    return 0


# ---------------------------------------------------------------- update check
def version_tuple(text):
    parts = []
    for piece in re.split(r"[.\-+]", str(text or ""))[:4]:
        match = re.match(r"\d+", piece)
        parts.append(int(match.group(0)) if match else 0)
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts)


def update_repo():
    """owner/name of the public GitHub repo that publishes app versions."""
    repo = (cfg("update", "repo", "") or "").strip().strip("/")
    return repo


def github_release():
    """Latest published GitHub release: {version, notes, url, name, size}."""
    repo = update_repo()
    if not repo:
        return None, "ریپو در [update] → repo تنظیم نشده"
    api = f"https://api.github.com/repos/{repo}/releases/latest"
    req = urllib.request.Request(api, headers={"User-Agent": "AI-Tool-updater",
                                               "Accept": "application/vnd.github+json"})
    try:
        with opener(True).open(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        return None, f"گیت‌هاب پاسخ {exc.code} داد (ریپو عمومی است و ریلیز منتشر شده؟)"
    except Exception as exc:
        return None, f"اتصال به گیت‌هاب ناموفق: {exc}"
    version = str(data.get("tag_name") or data.get("name") or "").strip().lstrip("vV")
    if not version:
        return None, "شمارهٔ نسخه در ریلیز گیت‌هاب پیدا نشد"
    assets = data.get("assets") or []
    asset = next((a for a in assets if str(a.get("name", "")).lower().endswith(".exe")), None)
    if asset is None:
        asset = next((a for a in assets if str(a.get("name", "")).lower().endswith(".zip")), None)
    return {"version": version, "notes": (data.get("body") or "").strip(),
            "url": (asset or {}).get("browser_download_url", ""),
            "name": (asset or {}).get("name", ""),
            "size": (asset or {}).get("size") or 0}, ""


def download_file(url, path, label=""):
    """Download an update package and report real progress (0-100)."""
    req = urllib.request.Request(url, headers={"User-Agent": "AI-Tool-updater"})
    with opener(True).open(req, timeout=90) as resp, open(path, "wb") as out:
        total = int(resp.headers.get("Content-Length") or 0)
        done = 0
        while True:
            chunk = resp.read(262144)
            if not chunk:
                break
            out.write(chunk)
            done += len(chunk)
            report_progress(10 + (85 * done / total if total else 0),
                            f"دانلود {label}".strip())
    return os.path.getsize(path)


def latest_version():
    """(info, error): the GitHub release first, then the optional feed_url."""
    if update_repo():
        payload, error = github_release()
        if not error:
            return payload, ""
        info(f"گیت‌هاب: {error}")
    url = cfg_opt("update", "feed_url")
    if not url:
        return None, "منبع نسخه تنظیم نشده (بخش [update] → repo یا feed_url)"
    try:
        with opener(True).open(url, timeout=25) as resp:
            raw = resp.read().decode("utf-8", "replace")
    except Exception as exc:
        return None, f"خواندن فایل نسخه ناموفق: {exc}"
    version, notes = "", ""
    try:
        data = json.loads(raw)
        version = str(data.get("version") or data.get("latest") or data.get("tag_name") or "")
        notes = str(data.get("notes") or data.get("url") or "")
    except Exception:
        match = re.search(r"v?(\d+(?:\.\d+)+)", raw)
        version = match.group(1) if match else ""
    version = version.strip().lstrip("vV")
    if not version:
        return None, "در فایل نسخه شمارهٔ نسخه پیدا نشد"
    return {"version": version, "notes": notes}, ""


def cmd_update(install=False):
    """Ask GitHub for the newest release; with install=True download and launch it."""
    print("\n── به‌روزرسانی برنامه ─────────────────")
    info(f"نسخهٔ فعلی: {APP_VERSION}" + (f" · ریپو: {update_repo()}" if update_repo() else ""))
    report_progress(5, "بررسی نسخهٔ تازه")
    payload, error = latest_version()
    if error:
        bad(error)
        info("بخش [update] → repo را مثل owner/name بنویس، یا feed_url را پر کن")
        return 1
    ok(f"آخرین نسخهٔ منتشرشده: {payload['version']}")
    if version_tuple(payload["version"]) <= version_tuple(APP_VERSION):
        report_progress(100, "برنامه به‌روز است")
        ok("نسخهٔ نصب‌شده به‌روز است")
        return 0
    warn(f"نسخهٔ تازه‌تر موجود است: {APP_VERSION} → {payload['version']}")
    for line in (payload.get("notes") or "").splitlines()[:6]:
        if line.strip():
            info(line.strip())
    notify("AI Tool — نسخهٔ تازه", f"نسخهٔ {payload['version']} آماده است (نسخهٔ فعلی {APP_VERSION})")
    if not install:
        info("برای دانلود و اجرای نصب: python agtool.py update install")
        return 0
    if not payload.get("url"):
        bad("در آن ریلیز، فایل نصب (exe) ضمیمه نشده")
        return 1
    name = payload.get("name") or f"AI-Tool-Setup-{payload['version']}.exe"
    dest = os.path.join(tempfile.gettempdir(), name)
    report_progress(10, f"دانلود نسخهٔ {payload['version']}")
    try:
        size = download_file(payload["url"], dest, payload["version"])
    except Exception as exc:
        bad(f"دانلود ناموفق: {exc}")
        return 1
    if payload.get("size") and size < int(payload["size"]):
        bad(f"فایل ناقص دانلود شد ({size} از {payload['size']} بایت)")
        return 1
    ok(f"دانلود شد: {size / 1048576:.1f} مگابایت → {dest}")
    report_progress(100, "آمادهٔ نصب")
    try:
        subprocess.Popen([dest], cwd=os.path.dirname(dest))
    except Exception as exc:
        bad(f"اجرای نصب‌کننده ممکن نشد: {exc}")
        return 1
    ok("نصب‌کننده باز شد — نصب که تمام شد، برنامه را دوباره باز کن")
    return 0


# ---------------------------------------------------------------- quota watch
def quota_status():
    port, csrf = find_hub()
    if not port:
        return None
    us = hub_call(port, csrf, "GetUserStatus", timeout=60).get("userStatus", {})
    if not us:
        return None
    plan = (us.get("planStatus", {}).get("planInfo", {}) or {}).get("planName", "?")
    return {"email": us.get("email", ""), "plan": plan,
            "credits": us.get("planStatus", {}).get("availablePromptCredits")}


def check_quota(force=False):
    """Read the remaining credits every N minutes (no IP change) and warn when low."""
    try:
        every = max(5, int(cfg("quota", "interval_minutes", "60")))
    except ValueError:
        every = 60
    try:
        below = int(cfg("quota", "warn_below", "50"))
    except ValueError:
        below = 50
    st = load_state()
    last = float(st.get("quota_ts") or 0)
    if not force and time.time() - last < every * 60:
        return st.get("quota")
    info_now = quota_status()
    if not info_now:
        return st.get("quota")
    st["quota"] = dict(info_now, ts=int(time.time()))
    st["quota_ts"] = time.time()
    save_state(st)
    credits = info_now.get("credits")
    if below > 0 and isinstance(credits, (int, float)) and credits < below:
        notify("AI Tool — اعتبار آنتی‌گرویتی کم شد",
               f"اعتبار باقی‌مانده: {credits} (هشدار زیر {below})")
    return st["quota"]


def cmd_quota(force=True):
    print("\n── اعتبار آنتی‌گرویتی ──────────────")
    data = check_quota(force=force)
    if not data:
        bad("خواندن اعتبار ممکن نشد (IDE بسته است یا سرویس زبان بالا نیامده)")
        return 1
    ok(f"حساب: {data.get('email') or '?'} · پلن {data.get('plan') or '?'}")
    ok(f"اعتبار باقی‌مانده: {data.get('credits')}")
    return 0


# ---------------------------------------------------------------- diagnostics bundle
def _os_text():
    return "\n".join([
        f"os: {sys.platform}",
        f"python: {sys.version.split()[0]}",
        f"frozen: {FROZEN}",
        f"app_dir: {HERE}",
        f"config: {CONFIG_FILE}",
        f"proxy: {PROXY or '(TUN)'}",
        f"socks: {SOCKS or '-'}",
        f"api: {MLM_API or '-'}",
        f"vpn_exe: {MLM_EXE or '-'}",
        f"ide_exe: {IDE_EXE}",
        f"pythonw: {find_pythonw()}",
        f"history: {len(load_state().get('switches') or [])} switches, "
        f"{len(load_state().get('ip_history') or [])} ip changes",
    ])


def cmd_bundle(path=""):
    print("\n── بستهٔ عیب‌یابی ─────────────────")
    import zipfile
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    path = path or os.path.join(DESKTOP if os.path.isdir(DESKTOP) else HERE,
                                f"AI-Tool-diagnostics-{stamp}.zip")
    status = collect_status(tries=1)
    try:
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as bundle:
            bundle.writestr("status.json", json.dumps(status, ensure_ascii=False, indent=1))
            bundle.writestr("agtool.ini", _strip_pin(_ini_text()))
            bundle.writestr("state.json", json.dumps(load_state(), ensure_ascii=False, indent=1))
            bundle.writestr("system.txt", _os_text())
            env = {k: os.environ.get(k, "") for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY")}
            bundle.writestr("proxy_env.json", json.dumps(env, ensure_ascii=False, indent=1))
            for name, extra in (("mlm_found_nodes.json", "nodes.json"),):
                try:
                    with open(os.path.join(HERE, name), encoding="utf-8") as handle:
                        bundle.writestr(extra, handle.read())
                except OSError:
                    pass
            for source, target in ((WATCH_LOG, "agtool-watch.log"),):
                try:
                    bundle.write(source, target)
                except OSError:
                    pass
    except OSError as exc:
        bad(f"ساخت فایل ZIP ممکن نشد: {exc}")
        return 1
    ok(f"بستهٔ عیب‌یابی ساخته شد: {path}")
    info(f"حجم: {os.path.getsize(path)//1024} کیلوبایت — بدون توکن و رمز؛ همین را برای پشتیبانی بفرست")
    return 0


# ---------------------------------------------------------------- proxy env vars
ENV_PROXY_KEYS = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY")


def env_proxy_read():
    keys = ",".join(f"'{k}'" for k in ENV_PROXY_KEYS)
    script = (f"foreach ($k in {keys}) {{ \"$k=\" + "
              "[Environment]::GetEnvironmentVariable($k,'User') }")
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                             capture_output=True, text=True, timeout=40, creationflags=NOWIN).stdout
    except Exception:
        return {}
    values = {}
    for line in (out or "").splitlines():
        key, _, value = line.partition("=")
        if key.strip() in ENV_PROXY_KEYS:
            values[key.strip()] = value.strip()
    return values


def cmd_envproxy(action="status"):
    print("\n── پروکسی برای ترمینال و ابزارها ────")
    action = (action or "status").lower()
    current = env_proxy_read()
    if action == "status":
        for key in ENV_PROXY_KEYS:
            value = current.get(key) or "—"
            print(f"  {key:<12} {value}")
        info("این متغیرها روی git / npm / pip / VS Code و ترمینال‌های تازه اثر می‌گذارند")
        return 0
    if action not in ("on", "off"):
        bad("on یا off را بده: envproxy on")
        return 1
    if action == "off":
        pairs = {key: "" for key in ENV_PROXY_KEYS}
        os.environ.pop("HTTP_PROXY", None)
    else:
        if not PROXY and not SOCKS:
            bad("حالت TUN است — اینترنت کل سیستم از VPN رد می‌شود و متغیر پروکسی لازم نیست")
            return 1
        value = PROXY or SOCKS
        pairs = {"HTTP_PROXY": value, "HTTPS_PROXY": value, "ALL_PROXY": SOCKS or value,
                 "NO_PROXY": "127.0.0.1,localhost"}
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
            os.environ[key] = pairs[key]
    lines = "\n".join(f"[Environment]::SetEnvironmentVariable('{k}','{v}','User')" for k, v in pairs.items())
    try:
        subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", lines],
                       capture_output=True, text=True, timeout=60, creationflags=NOWIN)
    except Exception as exc:
        bad(f"نوشتن متغیرها ناموفق: {exc}")
        return 1
    if action == "off":
        ok("متغیرهای پروکسی حذف شدند (ترمینال‌های باز باید دوباره باز شوند)")
    else:
        ok(f"متغیرهای پروکسی ست شدند → {pairs['HTTP_PROXY']}")
        info("هر ترمینال/برنامه‌ای که از قبل باز است، باید دوباره باز شود")
    return 0


# ---------------------------------------------------------------- PIN lock
def pin_hash(pin):
    return hashlib.sha256((PIN_SALT + str(pin)).encode("utf-8")).hexdigest()


def pin_problem(pin):
    raw = str(pin or "").strip()
    if not raw:
        return "PIN خالی است"
    if not raw.isdigit():
        return "PIN باید فقط عدد باشد"
    if len(raw) < 4:
        return "PIN باید حداقل ۴ رقم باشد"
    return ""


def pin_is_set():
    return bool(cfg_opt("security", "pin_hash"))


def pin_ok(pin):
    if not pin_is_set():
        return True
    return cfg_opt("security", "pin_hash") == pin_hash(pin)


def set_pin(pin):
    problem = pin_problem(pin)
    if problem:
        return problem
    if not write_ini_values("security", {"pin_hash": pin_hash(pin)}):
        return "نوشتن فایل تنظیمات ممکن نشد"
    return ""


def clear_pin(pin=""):
    if pin_is_set() and not pin_ok(pin):
        return "PIN فعلی درست نیست"
    if not write_ini_values("security", {"pin_hash": ""}):
        return "نوشتن فایل تنظیمات ممکن نشد"
    return ""


def cmd_pin(action="status", pin="", second=""):
    print("\n── قفل تنظیمات ─────────────────────")
    action = (action or "status").lower()
    if action == "status":
        (ok if pin_is_set() else warn)("قفل روشن است" if pin_is_set() else "قفل خاموش است")
        return 0
    if action == "set":
        if pin_is_set() and not pin_ok(second or pin):
            bad("قفل از قبل روشن است — برای عوض کردن: pin off <PIN فعلی> بعد pin set <PIN تازه>")
            return 1
        problem = set_pin(pin)
        if problem:
            bad(problem)
            return 1
        ok("قفل تنظیمات روشن شد — برای باز کردن فایل تنظیمات و اعمال، PIN لازم است")
        info("این قفل داخل برنامه است؛ هر کسی به فایل دسترسی داشته باشد می‌تواند با Notepad عوضش کند")
        return 0
    if action == "off":
        problem = clear_pin(pin)
        if problem:
            bad(problem)
            return 1
        ok("قفل تنظیمات خاموش شد")
        return 0
    bad(f"کار نامشخص: {action} — set / off / status")
    return 1


# ---------------------------------------------------------------- custom VPN
MAX_TPROXY_SCAN = 400


def listeners_by_pid():
    """{pid: set(ports)} of local LISTENING TCP sockets."""
    try:
        out = subprocess.run(["netstat", "-ano", "-p", "tcp"], capture_output=True, text=True,
                             timeout=60, creationflags=NOWIN).stdout
    except Exception:
        return {}
    rows = {}
    for line in (out or "").splitlines():
        parts = line.split()
        if len(parts) < 5 or parts[0].upper() != "TCP" or parts[3].upper() != "LISTENING":
            continue
        address, _, port = parts[1].rpartition(":")
        if not port.isdigit() or not parts[4].isdigit():
            continue
        if address.strip("[]") not in ("127.0.0.1", "0.0.0.0", "::", "::1"):
            continue
        rows.setdefault(int(parts[4]), set()).add(int(port))
    return rows


def pids_for_exe(exe_name):
    """PIDs of that program and of every child it spawned (VPN clients proxy in a child)."""
    name = os.path.splitext(os.path.basename(exe_name))[0].replace("'", "''")
    script = (
        "$all = Get-CimInstance Win32_Process | Select-Object ProcessId, ParentProcessId, Name; "
        f"$roots = $all | Where-Object {{ $_.Name -eq '{name}.exe' -or $_.Name -eq '{name}' }} "
        "| Select-Object -ExpandProperty ProcessId; "
        "$ids = New-Object System.Collections.Generic.List[int]; "
        "foreach ($r in $roots) { $ids.Add([int]$r) }; "
        "do { $added = $false; "
        "foreach ($p in $all) { if (($ids -contains [int]$p.ParentProcessId) -and "
        "(-not ($ids -contains [int]$p.ProcessId))) { $ids.Add([int]$p.ProcessId); $added = $true } } } "
        "while ($added); "
        "$ids | Sort-Object -Unique")
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                             capture_output=True, text=True, timeout=45, creationflags=NOWIN).stdout
    except Exception:
        return set()
    return {int(x) for x in (out or "").split() if x.strip().isdigit()}


def probe_http_proxy(port, timeout=8):
    proxy = f"http://127.0.0.1:{port}"
    handler = urllib.request.ProxyHandler({"http": proxy, "https": proxy})
    try:
        with urllib.request.build_opener(handler).open("https://www.google.com/generate_204",
                                                      timeout=timeout) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code
    except Exception:
        return 0


def probe_socks5(port, host="www.google.com", target=443, timeout=6):
    sock = socket.socket()
    sock.settimeout(timeout)
    try:
        sock.connect(("127.0.0.1", port))
        sock.sendall(b"\x05\x01\x00")
        if sock.recv(2) != b"\x05\x00":
            return False
        payload = b"\x05\x01\x00\x03" + bytes([len(host)]) + host.encode() + target.to_bytes(2, "big")
        sock.sendall(payload)
        reply = sock.recv(4)
        return len(reply) == 4 and reply[0] == 5 and reply[1] == 0
    except Exception:
        return False
    finally:
        sock.close()


def detect_vpn_ports(exe_path, wait=45):
    """Launch-agnostic port discovery: look at the client's own listening ports and probe them."""
    name = os.path.basename(exe_path or "")
    if not name:
        return None
    if name.lower().endswith((".lnk", ".url")):
        info("شورتکات انتخاب شد — برای تشخیص پورت، خودِ فیلترشکن باید باز باشد")
        return None
    pids = pids_for_exe(name)
    if not pids:
        info(f"«{name}» اجرا نیست — باید اول باز شود")
        return None
    listeners = listeners_by_pid()
    ports = sorted({port for pid in pids for port in listeners.get(pid, set())})
    api_port = mlm_api_port()
    ports = [p for p in ports if p != api_port][:MAX_TPROXY_SCAN]
    if not ports:
        bad("هیچ پورت محلی برای این برنامه پیدا نشد (شاید TUN است و پورت پروکسی ندارد)")
        return None
    info("پورت‌های محلی برنامه: " + ", ".join(str(p) for p in ports))
    http_ok = [port for port in ports if probe_http_proxy(port, timeout=6) in (200, 204)]
    socks_ok = [port for port in ports if probe_socks5(port)]
    if not http_ok and not socks_ok:
        bad("هیچ‌کدام از پورت‌ها به‌عنوان پروکسی جواب ندادند — داخل خود فیلترشکن یک بار وصل شو و دوباره تست کن")
        return None
    if http_ok:
        ok("پروکسی HTTP روی پورت: " + ", ".join(str(p) for p in http_ok))
    if socks_ok:
        ok("پروکسی SOCKS روی پورت: " + ", ".join(str(p) for p in socks_ok))
    mixed = [p for p in http_ok if p in socks_ok]
    # a client that answers both is a mixed port: use its neighbour for HTTP when possible
    if len(http_ok) > 1:
        http_only = [p for p in http_ok if p not in socks_ok]
        http_port = http_only[0] if http_only else http_ok[-1]
    else:
        http_port = http_ok[0] if http_ok else (mixed[0] if mixed else None)
    socks_port = socks_ok[0] if socks_ok else (mixed[0] if mixed else None)
    return {"http": http_port, "socks": socks_port}


def add_custom_preset(name, exe_path, http_port="", socks_port="", api=""):
    name = (name or os.path.splitext(os.path.basename(exe_path or ""))[0] or "فیلترشکن من").strip()
    base = "custom-" + (re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:24] or "vpn")
    existing = {p["id"] for p in load_custom_presets()} | {p["id"] for p in VPN_PRESETS}
    preset_id, counter = base, 2
    while preset_id in existing:
        preset_id = f"{base}-{counter}"
        counter += 1
    preset = {"id": preset_id, "name": name, "custom": True, "app": exe_path or "", "api": api or ""}
    if http_port:
        preset["http"] = f"http://127.0.0.1:{int(http_port)}"
    else:
        preset["http"] = ""
    if socks_port:
        preset["socks"] = f"socks5h://127.0.0.1:{int(socks_port)}"
    elif http_port:
        preset["socks"] = f"socks5h://127.0.0.1:{int(http_port)}"
    else:
        preset["socks"] = ""
    items = load_custom_presets() + [preset]
    if not save_custom_presets(items):
        return None
    return preset


# ---------------------------------------------------------------- cleaner parity
def antigravity_cache_dirs():
    base = os.path.join(os.environ.get("APPDATA", ""), "Antigravity IDE")
    return [os.path.join(base, name) for name in APP_CACHE_DIRS]


def path_size(path):
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                continue
    return total


def cmd_clean(dry=True):
    """Clear only Antigravity caches/logs (429 resets, slow-start fixes). Settings stay."""
    print("\n── پاک‌سازی کش آنتی‌گرویتی ─────────")
    total = 0
    for path in antigravity_cache_dirs():
        if not os.path.isdir(path):
            continue
        size = path_size(path)
        total += size
        label = f"{os.path.basename(path):<18} {size // 1024 // 1024} مگابایت"
        if dry:
            print(f"    {label}")
            continue
        removed = 0
        for entry in os.listdir(path):
            target = os.path.join(path, entry)
            try:
                if os.path.isdir(target):
                    shutil.rmtree(target, ignore_errors=True)
                else:
                    os.remove(target)
                removed += 1
            except OSError:
                continue
        print(f"    {label} → {removed} مورد پاک شد")
    if not total:
        info("کش آنتی‌گرویتی پیدا نشد (شاید IDE هنوز اجرا نشده)")
        return 0
    if dry:
        info(f"مجموع کش: حدود {total // 1024 // 1024} مگابایت — با clean --yes واقعاً پاک می‌شود")
        info("فقط کش و لاگ پاک می‌شود؛ پروژه‌ها، تنظیمات، اکستنشن‌ها و ورود تو دست‌نخورده می‌مانند")
        return 0
    ok(f"پاک‌سازی تمام شد — حدود {total // 1024 // 1024} مگابایت")
    info("IDE را دوباره باز کن؛ اگر پیام ورود داد، فقط یک بار وارد شو")
    return 0


def cmd_kill():
    print("\n── بستن پروسه‌های آنتی‌گرویتی ────────")
    killed = []
    for name in ("Antigravity IDE.exe", "agy.exe", "language_server.exe", "language_server_windows_x64.exe"):
        result = subprocess.run(["taskkill", "/F", "/IM", name], capture_output=True, text=True,
                                creationflags=NOWIN)
        if result.returncode == 0:
            killed.append(name)
    if killed:
        ok("بسته شد: " + " · ".join(killed))
        info("پروسه‌های یتیم language_server هم پاک شدند")
    else:
        info("پروسهٔ آنتی‌گرویتی باز نبود")
    return 0


def cmd_doctor():
    """Read-only health audit (cleaner 'doctor' equivalent) — changes nothing."""
    print("\n── عیب‌یاب ────────────────────────")
    info(f"AI Tool {APP_VERSION} · {sys.platform}")
    for label, path in (("Antigravity IDE", IDE_EXE), ("فیلترشکن", MLM_EXE), ("Chrome", chrome_path() or "")):
        (ok if path and os.path.exists(path) else bad)(f"{label}: {path or 'تنظیم نشده'}")
    for label, exe in (("IDE در حال اجرا", "Antigravity IDE.exe"), ("agy", "agy.exe")):
        running = bool(pids_for_exe(exe))
        (ok if running else info)(f"{label}: {'بله' if running else 'خیر'}")
    for port in [p for p in proxy_ports() if p]:
        (ok if port_open(port) else bad)(f"پورت {port}: " + ("باز" if port_open(port) else "بسته"))
    watchers = watchdog_pid()
    (ok if watchers else warn)(f"نظارت خودکار: {'روشن' if watchers else 'خاموش'}")
    env = env_proxy_read()
    (ok if env.get("HTTP_PROXY") else info)(f"پروکسی محیط کاربر: {env.get('HTTP_PROXY') or 'ست نشده'}")
    inner = read_token()
    exp = token_expiry(inner)
    left = (exp - datetime.datetime.now(datetime.timezone.utc)).total_seconds() if exp else -1
    if not inner:
        warn("توکن آنتی‌گرویتی: موجود نیست")
    elif left > 300:
        ok(f"توکن آنتی‌گرویتی: معتبر ({int(left // 60)} دقیقه)")
    else:
        warn(f"توکن آنتی‌گرویتی: نزدیک انقضا ({int(max(left, 0) // 60)} دقیقه)")
    hub_port, _csrf = find_hub()
    (ok if hub_port else info)(f"سرویس زبان: {'در حال اجرا' if hub_port else 'اجرا نیست'}")
    try:
        credits = (check_quota() or {}).get("credits")
    except Exception:
        credits = None
    if credits is not None:
        ok(f"اعتبار باقی‌مانده: {credits}")
    info(f"قفل تنظیمات: {'روشن' if pin_is_set() else 'خاموش'} · نسخه: {APP_VERSION}")
    feed = cfg_opt("update", "feed_url")
    info(f"بررسی به‌روزرسانی: {'تنظیم شده' if feed else 'تنظیم نشده'} · پروفایل‌های دلخواه: {len(load_custom_presets())}")
    return 0


def cmd_shortcut():
    """Desktop shortcut that starts the IDE with the proxy flags."""
    print("\n── شورتکات دسکتاپ ───────────────────")
    if not os.path.exists(IDE_EXE):
        bad("Antigravity IDE نصب نیست")
        return 1
    if not os.path.isdir(DESKTOP):
        bad(f"پوشهٔ دسکتاپ پیدا نشد: {DESKTOP}")
        return 1
    lnk = os.path.join(DESKTOP, "Antigravity (AI Tool).lnk")
    args = f"--proxy-server={PROXY}" if PROXY else ""
    script = ("$w = New-Object -ComObject WScript.Shell; "
              f"$s = $w.CreateShortcut('{lnk.replace(chr(39), chr(39) * 2)}'); "
              f"$s.TargetPath = '{IDE_EXE.replace(chr(39), chr(39) * 2)}'; "
              f"$s.Arguments = '{args}'; "
              f"$s.WorkingDirectory = '{os.path.dirname(IDE_EXE).replace(chr(39), chr(39) * 2)}'; "
              f"$s.IconLocation = '{IDE_EXE.replace(chr(39), chr(39) * 2)}'; "
              "$s.Save()")
    subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                   capture_output=True, text=True, timeout=60, creationflags=NOWIN)
    if os.path.exists(lnk):
        ok(f"شورتکات ساخته شد: {lnk}")
        info("با این شورتکات، IDE با پروکسی فعلی باز می‌شود")
        return 0
    bad("ساخت شورتکات ناموفق")
    return 1


def start_exe(path):
    """Start any VPN client (exe or shortcut), asking for elevation when needed."""
    path = os.path.expandvars(path or "")
    if not path:
        return False
    if path.lower().endswith((".lnk", ".url")):
        try:
            os.startfile(path)  # noqa: S606 - launching a user-chosen shortcut
            return True
        except OSError:
            return False
    try:
        subprocess.Popen([path], cwd=os.path.dirname(path) or None, creationflags=0x00000008)
        return True
    except OSError as exc:
        if getattr(exc, "winerror", None) == 740:
            warn("اجرا به دسترسی مدیر نیاز دارد — پنجرهٔ UAC را تأیید کن")
            subprocess.Popen(["powershell", "-NoProfile", "-Command",
                              f"Start-Process -FilePath '{path}' -Verb RunAs"], creationflags=NOWIN)
            return True
        bad(f"اجرای برنامه ممکن نشد: {exc}")
        return False


def cmd_launchvpn():
    print("\n── اجرای فیلترشکن ───────────────────")
    if PROXY and proxy_ports_open():
        ok("پورت پروکسی از قبل باز است — نیازی به اجرا نیست")
        return 0
    if not MLM_EXE:
        bad("مسیر فیلترشکن در agtool.ini تنظیم نشده (بخش [vpn] → app_exe)")
        info("از دکمهٔ «فیلترشکن دلخواه…» یک فایل اجرایی یا شورتکات انتخاب کن")
        return 1
    if not os.path.exists(os.path.expandvars(MLM_EXE)):
        bad(f"فایل فیلترشکن پیدا نشد: {MLM_EXE}")
        return 1
    info(f"اجرای: {MLM_EXE}")
    if not start_exe(MLM_EXE):
        return 1
    if focus_app_window(os.path.basename(MLM_EXE), tries=10):
        ok("پنجرهٔ فیلترشکن باز شد و جلو آمد")
    for _ in range(20):
        if (PROXY and proxy_ports_open()) or (not PROXY and tunnel_ok()):
            ok("فیلترشکن بالا آمد و مسیر پروکسی جواب می‌دهد")
            return 0
        time.sleep(2)
    warn("برنامه باز شد ولی پورت پروکسی جواب نداد — داخل خودش یک بار وصل شو")
    return 0


def cmd_vpn_custom(name, exe_path, http_port="", socks_port=""):
    print("\n── فیلترشکن دلخواه ─────────────────")
    exe_path = os.path.expandvars(exe_path or "")
    if not exe_path or not os.path.exists(exe_path):
        bad(f"فایل پیدا نشد: {exe_path}")
        return 1
    if not (http_port or socks_port):
        info("پورت‌ها داده نشد — تلاش برای تشخیص خودکار ...")
        if not pids_for_exe(os.path.basename(exe_path)):
            start_exe(exe_path)
            for _ in range(20):
                if pids_for_exe(os.path.basename(exe_path)):
                    break
                time.sleep(2)
            time.sleep(6)
        found = detect_vpn_ports(exe_path)
        if not found:
            bad("تشخیص خودکار پورت ممکن نشد — پورت را دستی بده")
            info("مثال: python agtool.py vpn custom \"نام\" \"C:\\path\\app.exe\" 10809 10808")
            return 1
        http_port = http_port or found.get("http") or ""
        socks_port = socks_port or found.get("socks") or ""
    preset = add_custom_preset(name, exe_path, http_port, socks_port)
    if not preset:
        bad("ذخیرهٔ پریست ممکن نشد")
        return 1
    ok(f"پریست «{preset['name']}» اضافه شد (id: {preset['id']})")
    if apply_vpn(preset["id"]):
        ok("در agtool.ini ذخیره شد — برای اعمال، برنامه را دوباره باز کن")
    return 0


def menu():
    while True:
        print(f"""
{GREEN}╭──────────────────────────────────────────╮
│  AI Tool — Google + Antigravity (VPN)    │
╰──────────────────────────────────────────╯{END}
  1) باز کردن Google Flow در کروم
  2) اجرای Antigravity (با ورود و پروکسی)
  3) وضعیت (تونل، حساب، کوota)
  4) تعمیر تونل (انتخاب نود سالم)
  5) نظارت خودکار (هر ۲ دقیقه چک و تعویض نود)
  6) استارت خودکار نظارت با ویندوز (روشن/خاموش)
  7) تست دستی قبل از باز کردن (هیچ تغییری نمی‌دهد)
  8) گزارش تاریخچهٔ IP و تعویض نود
  9) انتخاب فیلترشکن (تشخیص/پریست)
  0) خروج""")
        try:
            choice = input("  انتخاب: ").strip()
        except (EOFError, KeyboardInterrupt):
            return 0
        if choice == "1":
            cmd_flow()
        elif choice == "2":
            cmd_ide()
        elif choice == "3":
            cmd_status()
        elif choice == "4":
            cmd_heal()
        elif choice == "5":
            try:
                secs = input("  فاصلهٔ چک به ثانیه [120]: ").strip() or "120"
                cmd_watch(int(secs))
            except ValueError:
                warn("عدد نامعتبر")
        elif choice == "7":
            cmd_preflight()
        elif choice == "8":
            cmd_history()
        elif choice == "9":
            cmd_vpn()
            pick = input("  نام پریست برای اعمال (خالی = بدون تغییر): ").strip().lower()
            if pick:
                cmd_vpn(pick)
        elif choice == "6":
            cmd_autostart("status")
            ans = input("  استارت خودکار با ویندوز روشن باشد؟ (y/n): ").strip().lower()
            if ans == "y":
                cmd_autostart("on")
            elif ans == "n":
                cmd_autostart("off")
        elif choice in ("0", "q", ""):
            return 0


if __name__ == "__main__":
    arg = sys.argv[1].lower() if len(sys.argv) > 1 else ""
    if FROZEN and not arg:
        # a windowed exe has no console: a bare double-click opens the GUI
        try:
            import agtool_gui
            agtool_gui.main()
        except Exception:
            pass
        sys.exit(0)
    table = {"flow": cmd_flow, "ide": cmd_ide, "status": cmd_status,
             "preflight": cmd_preflight, "history": cmd_history, "json": cmd_json,
             "connect": cmd_connect}
    if arg in table:
        sys.exit(table[arg]())
    if arg == "heal":
        sys.exit(cmd_heal("force" in [a.lower() for a in sys.argv[2:]]))
    if arg == "watch":
        secs = 0  # 0 = take [watch] interval_seconds from agtool.ini
        for extra in sys.argv[2:]:
            if extra.isdigit():
                secs = int(extra)
        sys.exit(cmd_watch(secs))
    if arg == "autostart":
        mode = sys.argv[2].lower() if len(sys.argv) > 2 else "status"
        sys.exit(cmd_autostart(mode))
    if arg == "config":
        sys.exit(cmd_config("open" in [a.lower() for a in sys.argv[2:]]))
    if arg == "profile":
        rest = sys.argv[2:]
        fresh = "--fresh" in [a.lower() for a in rest]
        rest = [a for a in rest if not a.startswith("--")]
        sys.exit(cmd_profile(rest[0] if rest else "export", rest[1] if len(rest) > 1 else "", fresh))
    if arg == "nodes":
        limit = next((int(a) for a in sys.argv[2:] if a.isdigit()), 0)
        sys.exit(cmd_nodes(limit))
    if arg == "update":
        sys.exit(cmd_update(install="install" in [a.lower() for a in sys.argv[2:]]))
    if arg == "bundle":
        sys.exit(cmd_bundle(sys.argv[2] if len(sys.argv) > 2 else ""))
    if arg == "envproxy":
        sys.exit(cmd_envproxy(sys.argv[2] if len(sys.argv) > 2 else "status"))
    if arg == "pin":
        rest = sys.argv[2:]
        sys.exit(cmd_pin(rest[0] if rest else "status", rest[1] if len(rest) > 1 else "",
                         rest[2] if len(rest) > 2 else ""))
    if arg == "quota":
        sys.exit(cmd_quota())
    if arg == "doctor":
        sys.exit(cmd_doctor())
    if arg == "clean":
        sys.exit(cmd_clean(dry="--yes" not in [a.lower() for a in sys.argv[2:]]))
    if arg == "kill":
        sys.exit(cmd_kill())
    if arg == "shortcut":
        sys.exit(cmd_shortcut())
    if arg == "launchvpn":
        sys.exit(cmd_launchvpn())
    if arg == "vpn":
        rest = sys.argv[2:]
        if rest and rest[0].lower() == "custom" and len(rest) > 2:
            sys.exit(cmd_vpn_custom(rest[1], rest[2], rest[3] if len(rest) > 3 else "",
                                    rest[4] if len(rest) > 4 else ""))
        if rest and rest[0].lower() == "apply" and len(rest) > 1:
            ports = [a for a in rest[2:] if a.strip()]
            sys.exit(cmd_vpn(rest[1].lower(), ports[0] if ports else "",
                             ports[1] if len(ports) > 1 else ""))
        sys.exit(cmd_vpn(rest[0].lower() if rest else ""))
    sys.exit(menu())
