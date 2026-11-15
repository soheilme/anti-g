#!/usr/bin/env python3
"""نمایش درست متن فارسی در ترمینال ویندوز.

ترمینال ویندوز (cmd / Windows Terminal) حروف فارسی را به هم می‌چسباند ولی
ترتیب راست‌به‌چپ را نمی‌فهمد، پس جمله برعکس دیده می‌شود. این ماژول خطوط را
با `arabic_reshaper` + `python-bidi` به ترتیب دیدِ درست تبدیل می‌کند و در
صورت نیاز آن‌ها را راست‌چین می‌کند؛ کدهای رنگ ANSI هم دست‌نخورده می‌مانند.

استفاده:
    import fa_rtl
    fa_rtl.install()          # همهٔ print()ها از این به بعد درست دیده می‌شوند
    fa_rtl.visual("سلام")     # تبدیل یک رشته
"""

from __future__ import annotations

import io
import os
import re
import shutil
import sys

_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_RTL = re.compile(r"[\u0590-\u05FF\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF"
                  r"\uFB1D-\uFDFF\uFE70-\uFEFF]")
_MARK = "\ue000%d\ue001"

_libs_cache = None


def has_rtl(text):
    """آیا در متن حرف فارسی/عربی هست؟"""
    return bool(_RTL.search(text))


def _libs():
    global _libs_cache
    if _libs_cache is None:
        try:
            from bidi.algorithm import get_display
            from arabic_reshaper import reshape
            _libs_cache = (get_display, reshape)
        except Exception:
            _libs_cache = False
    return _libs_cache or None


def available():
    """آیا کتابخانه‌های لازم نصب هستند؟"""
    return _libs() is not None


def _keep_ansi(text):
    codes = []

    def keep(match):
        codes.append(match.group(0))
        return _MARK % (len(codes) - 1)

    return _ANSI.sub(keep, text), codes


def _restore_ansi(text, codes):
    for index, code in enumerate(codes):
        text = text.replace(_MARK % index, code)
    return text


def visual(text):
    """متن را به ترتیبِ درستِ نمایش تبدیل می‌کند (بدون تغییر طول دیداری)."""
    libs = _libs()
    if libs is None or not has_rtl(text):
        return text
    get_display, reshape = libs
    protected, codes = _keep_ansi(text)
    try:
        out = get_display(reshape(protected))
    except Exception:
        return text
    return _restore_ansi(out, codes)


def display_width(text):
    """طول دیداری خط (بدون کدهای رنگ)."""
    return len(_ANSI.sub("", text))


def render_line(line, width=None, align=True):
    """یک خط را برای چاپ آماده می‌کند: ترتیب درست + راست‌چین."""
    if not has_rtl(line):
        return line
    if line.lstrip().startswith(("{", "[")):      # خروجی JSON را دست نزن
        return line
    out = visual(line)
    if align and width:
        pad = width - display_width(out) - 1
        if pad > 0:
            out = " " * pad + out
    return out


def _terminal_width(stream):
    try:
        if stream is not None and stream.isatty():
            return shutil.get_terminal_size((80, 24)).columns
    except Exception:
        pass
    return None


class RtlStream(io.TextIOBase):
    """جایگزین sys.stdout که خط‌به‌خط خروجی را درست می‌چیند."""

    def __init__(self, stream, width=None):
        self._stream = stream
        self.width = width
        self._buf = ""

    # --- گذر به استریم اصلی -------------------------------------------------
    def __getattr__(self, name):
        return getattr(self._stream, name)

    @property
    def encoding(self):
        return getattr(self._stream, "encoding", "utf-8")

    @property
    def buffer(self):
        return getattr(self._stream, "buffer", None)

    def isatty(self):
        try:
            return self._stream.isatty()
        except Exception:
            return False

    def fileno(self):
        return self._stream.fileno()

    def writable(self):
        return True

    # --- نوشتن ---------------------------------------------------------------
    def _emit(self, line):
        self._stream.write(render_line(line, self.width) + "\n")

    def write(self, text):
        if not isinstance(text, str):
            text = str(text)
        self._buf += text
        while self._buf:
            cut = min([i for i in (self._buf.find("\n"), self._buf.find("\r")) if i >= 0],
                      default=-1)
            if cut < 0:
                break
            line = self._buf[:cut]
            rest = self._buf[cut + 1:]
            if self._buf[cut] == "\r" and rest.startswith("\n"):   # \r\n یک خط است
                rest = rest[1:]
            self._buf = rest
            self._emit(line)
        return len(text)

    def flush(self):
        if self._buf:
            self._emit(self._buf)
            self._buf = ""
        try:
            self._stream.flush()
        except Exception:
            pass


def install(stream=None, width=None):
    """sys.stdout/sys.stderr را طوری عوض می‌کند که فارسی درست دیده شود."""
    if os.environ.get("AGT_RTL", "1").strip().lower() in ("0", "off", "no", "false"):
        return None
    if not available():
        return None
    if width is None:
        try:
            width = int(os.environ.get("AGT_RTL_WIDTH", "0") or 0) or None
        except ValueError:
            width = None
    if width is None:
        width = _terminal_width(stream or sys.stdout)
    if not width:                                  # خروجی به فایل/پایپ: دست نزن
        return None
    if stream is None:
        if sys.stdout is not None and not isinstance(sys.stdout, RtlStream):
            sys.stdout = RtlStream(sys.stdout, width)
        if sys.stderr is not None and not isinstance(sys.stderr, RtlStream):
            sys.stderr = RtlStream(sys.stderr, width)
        return sys.stdout
    return RtlStream(stream, width)


def _selftest():
    ok = True
    if not has_rtl("سلام"):
        print("FAIL: has_rtl")
        ok = False
    if has_rtl("hello 123"):
        print("FAIL: false positive on latin")
        ok = False
    if visual("hello 123") != "hello 123":
        print("FAIL: latin should pass through")
        ok = False
    if render_line('{"a": "سلام"}', width=80) != '{"a": "سلام"}':
        print("FAIL: json should pass through")
        ok = False
    if display_width("\x1b[92mOK\x1b[0m") != 2:
        print("FAIL: display_width")
        ok = False
    if available():
        out = visual("\x1b[92mOK\x1b[0m   اتصال برقرار است")
        if "\x1b[92m" not in out or "\x1b[0m" not in out:
            print("FAIL: ansi codes lost")
            ok = False
        if _RTL.findall(out) and out == "\x1b[92mOK\x1b[0m   اتصال برقرار است":
            print("FAIL: rtl line not reordered")
            ok = False
    stream = io.StringIO()
    wrapped = RtlStream(stream, width=0)
    wrapped.write("خط اول\nخط دوم\r\nپایان")
    wrapped.flush()
    lines = stream.getvalue().splitlines()
    if len(lines) != 3 or lines[-1] != visual("پایان"):
        print("FAIL: stream splitting ->", lines)
        ok = False
    print("selftest:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(_selftest())
