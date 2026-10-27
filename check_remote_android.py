# /// script
# requires-python = ">=3.11"
# dependencies = ["pillow", "numpy", "websocket-client"]
# ///
"""遠隔を、Android の実機の Chrome から確かめる。USB でつなぎ、USB デバッグを許可しておく。

    uv run check_remote_android.py

ドックを開いて試験用の窓を 2 つ取り込み (1 つは最小の幅 600)、遠隔を 127.0.0.1 で始める。
スマホの 127.0.0.1:15900 を PC に届け (adb reverse)、スマホの Chrome で合言葉入りの URL を開く。
Chrome の開発用の口 (adb forward tcp:29222) から本物のタッチと、日本語の確定文字を送る。
- 縦向き・横向きで、ドックがスマホの画面の大きさになる
- タップ・長押し・日本語の入力がツールに届く。タブのタップで切り替わる
画面の向きの設定・開いたタブ・adb の転送は最後に戻す。人の設定・前回のタブには触れない (一時フォルダを使う)。
"""

import ctypes
import os
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from pathlib import Path

HOME = Path(tempfile.mkdtemp(prefix="wintabdock-android-"))
os.environ["WINTABDOCK_HOME"] = str(HOME)
NOVNC_CACHE = Path(os.environ.get("LOCALAPPDATA", ".")) / "WinTabDock"
if (NOVNC_CACHE / "novnc-1.6.0").exists():
    import shutil
    shutil.copytree(NOVNC_CACHE / "novnc-1.6.0", HOME / "novnc-1.6.0")

sys.path.insert(0, str(Path(__file__).resolve().parent / "experiments"))
import remote  # noqa: E402
import vnc_android_check as va  # noqa: E402  (adb とスマホの Chrome を動かす手順を借りる)
import vnc_probe_check as ck  # noqa: E402
import wintabdock as td  # noqa: E402

OUT = Path(__file__).resolve().parent / "experiments" / "out"


def tk_get(dock: td.Dock, fn):
    box = []
    dock.call_in_tk(lambda: box.append(fn()))
    return box[0] if box else None


def steps(dock: td.Dock) -> None:
    app1, h1 = ck.start_app(f"an{os.getpid()}a")
    app2, h2 = ck.start_app(f"an{os.getpid()}b", 600)
    rotation = (va.adb("shell", "settings", "get", "system", "accelerometer_rotation"),
                va.adb("shell", "settings", "get", "system", "user_rotation"))
    tab_info = tab = None
    try:
        dock.call_in_tk(lambda: dock.adopt("app1", h1))
        dock.call_in_tk(lambda: dock.adopt("app2", h2))
        dock.call_in_tk(lambda: dock.select(dock.tabs[0]))
        dock.call_in_tk(dock.start_remote)
        for _ in range(100):
            if dock.remote_server:
                break
            time.sleep(0.1)
        url = dock.remote_server.url
        va.adb("reverse", f"tcp:{remote.DEFAULT_PORT}", f"tcp:{remote.DEFAULT_PORT}")
        va.adb("forward", f"tcp:{va.CDP_PORT}", "localabstract:chrome_devtools_remote")
        va.adb("shell", "input", "keyevent", "KEYCODE_WAKEUP")
        va.adb("shell", "am", "start", "-a", "android.intent.action.VIEW", "-p", "com.android.chrome", "-d", f"'{url}'")
        tab_info = va.find_tab()
        tab = va.Tab(tab_info["webSocketDebuggerUrl"])
        time.sleep(4)

        vw, vh, dpr = tab.js("[innerWidth, innerHeight, devicePixelRatio]")
        print(f"縦向き: ブラウザの中は {vw}x{vh}、devicePixelRatio {dpr}")
        fb = va.wait_fb(tab, vw, vh)
        va.screenshot("dock-1-portrait")
        ck.check("縦向き: ドックの内側がスマホの画面の大きさになる",
                 fb == (vw, vh) and remote.client_size(dock.dock_hwnd) == (vw, vh),
                 f"noVNC {fb} ドック {remote.client_size(dock.dock_hwnd)}")
        r = td.rect_of(dock.dock_hwnd)
        ck.check("つないでいる間、ドックは画面の外にある", r[0] >= td.user32.GetSystemMetrics(78), f"left={r[0]}")

        _, tx, ty, _, _ = td.RemoteSource(dock).layers()[1]
        tab.tap(tx + 140, ty + 110)
        title = ck.wait_title(h1, "count=1")
        ck.check("タップでツールのボタンを押せる", "count=1" in title, title)
        tab.tap(tx + 140, ty + 110, hold=0.6)
        title = ck.wait_title(h1, "count=2")
        ck.check("長押し (0.6 秒) でも押せる", "count=2" in title, title)
        tab.tap(tx + 210, ty + 190)
        time.sleep(0.5)
        tab.js("document.getElementById('noVNC_keyboardinput').focus()")
        time.sleep(0.5)
        tab.call("Input.insertText", text="abc")
        tab.call("Input.insertText", text="日本語")
        title = ck.wait_title(h1, "text=abc日本語 ")
        ck.check("文字と日本語の確定文字を打てる", "text=abc日本語 " in title, title)
        va.screenshot("dock-2-typed")
        tab.js("document.getElementById('noVNC_keyboardinput').blur()")
        time.sleep(1.0)

        # タブ (app2) を押す。ドックの帯の中の位置 (縮めていない画面)
        bx, by, bw, bh = td.rect_of(dock.tabs[1].button_hwnd)
        ox, oy = remote.client_origin(dock.dock_hwnd)
        tab.tap(bx - ox + bw // 2, by - oy + bh // 2)
        time.sleep(2.0)
        ck.check("タブのタップで切り替わる", dock.active is dock.tabs[1], dock.active.name if dock.active else "-")
        va.screenshot("dock-3-minwidth")
        view = dock.remote_server.view
        _, tx, ty, tw, _ = td.RemoteSource(dock).layers()[1]
        s = view.scale
        tab.tap(round((tx + 140) * s), round((ty + 110) * s))
        title = ck.wait_title(h2, "count=1")
        ck.check(f"最小の幅のツール (縮めて {s:.2f} 倍) の上でも押せる", "count=1" in title, title)

        va.adb("shell", "settings", "put", "system", "accelerometer_rotation", "0")
        va.adb("shell", "settings", "put", "system", "user_rotation", "1")
        time.sleep(3)
        vw2, vh2 = tab.js("[innerWidth, innerHeight]")
        print(f"横向き: ブラウザの中は {vw2}x{vh2}")
        fb = va.wait_fb(tab, vw2, vh2)
        va.screenshot("dock-4-landscape")
        ck.check("横向き: noVNC の画面が横長の大きさに変わる", vw2 > vh2 and fb == (vw2, vh2), f"noVNC {fb} / {vw2}x{vh2}")
        log = (HOME / "remote.log").read_text(encoding="utf-8")
        ck.check("Tk が止まらない", "応えない" not in log, "remote.log に「応えない」が無い")
    except Exception:
        traceback.print_exc()
        ck.check("途中で止まらない", False)
    finally:
        va.adb("shell", "settings", "put", "system", "accelerometer_rotation", rotation[0], check=False)
        va.adb("shell", "settings", "put", "system", "user_rotation", rotation[1], check=False)
        if tab:
            tab.close()
        if tab_info:
            try:
                va.urllib.request.urlopen(f"http://127.0.0.1:{va.CDP_PORT}/json/close/{tab_info['id']}", timeout=5).read()
            except OSError:
                pass
        va.adb("forward", "--remove", f"tcp:{va.CDP_PORT}", check=False)
        va.adb("reverse", "--remove", f"tcp:{remote.DEFAULT_PORT}", check=False)
        # 試験用の窓を先に閉じてから、ドックを閉じる (ドックを閉じるとこのプロセスが終わり、このスレッドも止まる)
        ck.close_apps([(app1, h1), (app2, h2)])
        dock.call_in_tk(dock.stop_remote)
        dock.call_in_tk(dock.quit, wait=False)


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    OUT.mkdir(exist_ok=True)
    if va.adb("get-state", check=False) != "device":
        raise SystemExit("実機がつながっていない、または USB デバッグが未許可")
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
    dock = td.Dock([], {"auto_adopt": False, "restore_session": False, "remote": {"host": "127.0.0.1"}})
    dock.root.geometry("900x600+150+120")
    threading.Thread(target=lambda: (time.sleep(1.0), steps(dock)), daemon=True).start()
    dock.root.mainloop()
    ng = [r for r in ck.results if not r[1]]
    print(f"\n{len(ck.results) - len(ng)}/{len(ck.results)} OK")
    sys.exit(1 if ng else 0)


if __name__ == "__main__":
    main()
