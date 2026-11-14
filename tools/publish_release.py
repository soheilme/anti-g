#!/usr/bin/env python3
"""انتشار نسخهٔ تازه روی گیت‌هاب (برای نگه‌دارندهٔ پروژه).

مراحل:
  1. نسخه را در `agtool.ini` بخش `[update]` → `version` بالا ببر.
  2. `AI-Tool-Setup.exe` را بیلد کن (pyinstaller + installer/build.rsp).
  3. این اسکریپت را اجرا کن:  python tools/publish_release.py

توکن از همان اعتبارنامهٔ گیت (git credential) خوانده می‌شود و چاپ نمی‌شود.
اگر پشت فیلترشکن هستی، پروکسی از `[vpn] http_proxy` برداشته می‌شود.
"""
import configparser
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ASSET = os.path.join(ROOT, "AI-Tool-Setup.exe")
INI = os.path.join(ROOT, "agtool.ini")


def settings():
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(INI, encoding="utf-8")
    repo = parser.get("update", "repo", fallback="").strip().strip("/")
    version = parser.get("update", "version", fallback="").strip()
    proxy = parser.get("vpn", "http_proxy", fallback="").strip() or None
    return repo, version, proxy


def credential():
    proc = subprocess.run(["git", "credential", "fill"], input="protocol=https\nhost=github.com\n\n",
                          capture_output=True, text=True, timeout=60)
    return next((line.split("=", 1)[1].strip() for line in proc.stdout.splitlines()
                 if line.startswith("password=")), "")


def api(method, url, token, proxy, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Authorization": "token " + token,
                                          "Accept": "application/vnd.github+json",
                                          "User-Agent": "anti-g-release"})
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": proxy, "https": proxy}) if proxy else urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=90) as resp:
            body = resp.read().decode("utf-8", "replace")
            return resp.status, (json.loads(body) if body.strip().startswith(("{", "[")) else body)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")[:400]


def upload(url, token, proxy):
    """curl با Expect خاموش: پروکسی‌ها روی 100-continue گیر می‌کنند و آپلود قطع می‌شود."""
    conf = (f'url = "{url}"\n'
            f'header = "Authorization: token {token}"\n'
            'header = "Content-Type: application/octet-stream"\n'
            'header = "User-Agent: anti-g-release"\n'
            'header = "Expect:"\n'
            "connect-timeout = 30\nmax-time = 1800\nprogress-bar\n")
    if proxy:
        conf += f'proxy = "{proxy}"\n'
    return subprocess.run(["curl", "--http1.1", "-K", "-", "--data-binary", "@" + ASSET],
                          input=conf, text=True, capture_output=True, timeout=1800)


def main():
    repo, version, proxy = settings()
    if not repo or not version:
        print("repo/version در agtool.ini تنظیم نشده")
        return 1
    if not os.path.exists(ASSET):
        print("AI-Tool-Setup.exe پیدا نشد — اول بیلد بگیر")
        return 1
    token = credential()
    if not token:
        print("اعتبارنامهٔ گیت‌هاب پیدا نشد (git credential)")
        return 1

    tag = "v" + version.lstrip("vV")
    status, release = api("GET", f"https://api.github.com/repos/{repo}/releases/tags/{tag}", token, proxy)
    if status == 404:
        status, release = api("POST", f"https://api.github.com/repos/{repo}/releases", token, proxy,
                              {"tag_name": tag, "name": f"ANTI G {version}",
                               "body": f"نسخهٔ {version}", "target_commitish": "main"})
    if status not in (200, 201):
        print("ساخت/خواندن ریلیز ناموفق:", status, release)
        return 1
    print("release:", release.get("html_url"))

    for asset in release.get("assets") or []:
        if asset.get("name") == "AI-Tool-Setup.exe":
            api("DELETE", f"https://api.github.com/repos/{repo}/releases/assets/{asset['id']}", token, proxy)
            print("asset قبلی پاک شد")

    url = (f"https://uploads.github.com/repos/{repo}/releases/{release['id']}/assets"
           "?name=AI-Tool-Setup.exe")
    result = upload(url, token, proxy)
    print("curl rc:", result.returncode)
    try:
        info = json.loads(result.stdout)
        print("uploaded:", info.get("name"), info.get("size"), info.get("state"))
        print(info.get("browser_download_url"))
    except Exception:
        print((result.stdout or "")[-300:], (result.stderr or "")[-300:])
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
