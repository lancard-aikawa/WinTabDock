"""Firefox をタブにしてスマホから使えるかを見る (2026-11-03。スマホで中身が真っ白だった)。

    uv run --with pillow --with numpy --with websocket-client --with zxing-cpp python experiments/firefox_remote_check.py
    (同じ引数に --no-occlusion を足すと、Firefox の窓の隠れ具合の追跡を切って流す)

人の画面で流す (隠したデスクトップでは Firefox は起動の画面から先を描かない)。試験用の Firefox を別のプロファイルで起動し、
押すと地の色が赤と緑で入れ替わるページを、試験のドックに入れてつなぐ。人の Firefox には触らない。
- ドックの画面に、ページの中身が写る (真っ白でない)
- 押すとページのクリックが起きる (題名の数字が増える)
- 押したあとの絵が、今のページの色になる。Firefox の既定の設定 (window_occlusion_tracking) では、画面の外の窓を
  隠れているとみなして描き直しを止めるので、ここは NG になる。--no-occlusion なら OK
"""
import ctypes
import os
import subprocess
import sys
import tempfile
import threading
import time
from ctypes import wintypes

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import check_remote as cr  # noqa: E402

td, ck, remote = cr.td, cr.ck, cr.remote
FIREFOX = r"C:\Program Files\Mozilla Firefox\firefox.exe"
TITLE = f"wtd-firefox-{os.getpid()}"
RED, GREEN = (204, 0, 0), (0, 160, 0)
PAGE = f"""<!doctype html><meta charset="utf-8"><title>{TITLE}</title>
<body style="margin:0;height:100vh;background:#c00" onclick="document.body.style.background=
document.body.style.background==='rgb(0, 160, 0)'?'#c00':'#00a000';document.title='{TITLE} '+(++n)">
<script>var n=0</script><h1 style="color:#fff;font:bold 40px sans-serif">tap me</h1></body>"""
PREFS = {"browser.aboutwelcome.enabled": "false", "trailhead.firstrun.didSeeAboutWelcome": "true",
         "datareporting.policy.dataSubmissionPolicyBypassNotification": "true",
         "termsofuse.bypassNotification": "true", "termsofuse.acceptedVersion": "999",
         "termsofuse.acceptedDate": '"1700000000000"', "browser.startup.homepage_override.mstone": '"ignore"',
         "browser.shell.checkDefaultBrowser": "false"}


def title(h: int) -> str:
    buf = ctypes.create_unicode_buffer(512)
    ctypes.windll.user32.GetWindowTextW(h, buf, 512)
    return buf.value


def find_window() -> int:
    found = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def each(h, _):
        cls = ctypes.create_unicode_buffer(64)
        ctypes.windll.user32.GetClassNameW(h, cls, 64)
        if cls.value == "MozillaWindowClass" and title(h).startswith(TITLE):
            found.append(h)
        return True
    ctypes.windll.user32.EnumWindows(each, 0)
    return found[0] if found else 0


def clicks(h: int) -> int:
    last = title(h).split(" — ")[0].split()[-1]
    return int(last) if last.isdigit() else 0


def stop_firefox(profile: str) -> None:
    """試験用の Firefox だけを止める (プロファイルのフォルダで見分ける)"""
    out = subprocess.run(["powershell", "-NoProfile", "-Command",
                          "Get-CimInstance Win32_Process -Filter \"Name='firefox.exe'\" | Where-Object { $_.CommandLine -like "
                          f"'*{profile}*' }} | ForEach-Object {{ $_.ProcessId }}"], capture_output=True, text=True).stdout
    for pid in out.split():
        subprocess.run(["taskkill", "/F", "/PID", pid], capture_output=True)


def steps(dock: td.Dock) -> None:
    profile = tempfile.mkdtemp(prefix="wtd-ff-")
    page = os.path.join(profile, "page.html")
    with open(page, "w", encoding="utf-8") as f:
        f.write(PAGE)
    prefs = dict(PREFS)
    if "--no-occlusion" in sys.argv:
        prefs["widget.windows.window_occlusion_tracking.enabled"] = "false"
    with open(os.path.join(profile, "user.js"), "w", encoding="utf-8") as f:
        f.writelines(f'user_pref("{k}", {v});\n' for k, v in prefs.items())
    subprocess.Popen([FIREFOX, "-no-remote", "-new-instance", "-profile", profile, page])
    try:
        h = 0
        for _ in range(150):
            h = find_window()
            if h:
                break
            time.sleep(0.2)
        cr.check("試験用の Firefox の窓が出る", bool(h))
        if not h:
            return
        time.sleep(3.0)
        dock.call_in_tk(lambda: dock.adopt("firefox", h))
        dock.call_in_tk(dock.start_remote)
        for _ in range(100):
            if dock.remote_server:
                break
            time.sleep(0.1)
        c = cr.Client(dock.prefs["remote_token"])
        try:
            c.set_size(411, 772)
            time.sleep(2.0)
            view = dock.remote_server.view
            _, tx, ty, tw, th = cr.RemoteSource_layers(dock)[1]
            x, y = round((tx + tw // 2) * view.scale), round((ty + th // 2) * view.scale)
            img = c.full_frame()
            cr.check("Firefox の中身 (赤い地) が写る (真っ白でない)", img.convert("RGB").getpixel((x, y)) == RED,
                     str(img.convert("RGB").getpixel((x, y))))
            for n in (1, 2):
                before = clicks(h)
                c.tap(x, y)
                time.sleep(1.5)
                img = c.full_frame()
                img.save(cr.OUT / f"firefox-tap{n}.png")
                want = GREEN if clicks(h) % 2 else RED
                got = img.convert("RGB").getpixel((x, y))
                cr.check(f"押すとページのクリックが起きる ({n} 回目)", clicks(h) == before + 1, title(h))
                cr.check(f"押したあとの絵が今のページの色になる ({n} 回目。既定の設定の Firefox では NG)", got == want,
                         f"写った色 {got} ページ {want}")
        finally:
            c.close()
    except Exception:
        import traceback
        traceback.print_exc()
        cr.check("途中で止まらない", False)
    finally:
        dock.call_in_tk(dock.stop_remote)
        time.sleep(1.0)
        stop_firefox(profile)
        dock.call_in_tk(dock.quit, wait=False)


sys.stdout.reconfigure(encoding="utf-8")
cr.ctypes.windll.shcore.SetProcessDpiAwareness(2)
cr.nofocus.start(lock=True)
dock = td.Dock([], {"auto_adopt": False, "restore_session": False, "remote": {"host": "127.0.0.1"}})
dock.root.geometry("900x600+150+120")
threading.Thread(target=lambda: (time.sleep(1.0), steps(dock)), daemon=True).start()
dock.root.mainloop()
ng = [r for r in cr.results if not r[1]]
print(f"\n{len(cr.results) - len(ng)}/{len(cr.results)} OK")
