# /// script
# requires-python = ">=3.11"
# dependencies = ["pillow", "numpy", "websocket-client"]
# ///
"""遠隔を、Android の実機の Chrome から確かめる。USB でつなぎ、USB デバッグを許可しておく。

    uv run check_remote_android.py                    # スマホの 127.0.0.1 を USB で PC に届ける (adb reverse)
    uv run check_remote_android.py --lan 192.168.x.x  # スマホは Wi-Fi で PC のこのアドレスにつなぐ (USB は試験の操作だけ)

ドックを開いて試験用の窓を 2 つ取り込み (1 つは最小の幅 600)、遠隔を 127.0.0.1 で始める。
スマホの 127.0.0.1:15900 を PC に届け (adb reverse)、スマホの Chrome で合言葉入りの URL を開く。
Chrome の開発用の口 (adb forward tcp:29222) から本物のタッチと、日本語の確定文字を送る。
- 縦向き・横向きで、ドックがスマホの画面の大きさになる
- タップ・長押し・日本語の入力がツールに届く。タブのタップで切り替わる
画面の向きの設定・開いたタブ・adb の転送は最後に戻す。人の設定・前回のタブには触れない (一時フォルダを使う)。
"""

import ctypes
import ctypes.wintypes
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
LAN = sys.argv[sys.argv.index("--lan") + 1] if "--lan" in sys.argv else None  # Wi-Fi でつなぐときの PC のアドレス


def tk_get(dock: td.Dock, fn):
    box = []
    dock.call_in_tk(lambda: box.append(fn()))
    return box[0] if box else None


def record_keys(dock: td.Dock) -> list[str]:
    """スマホから届いたキー (押した分) を控える。落ちたときに、noVNC が何を送ってきたかを出すため。"""
    keys: list[str] = []
    inp = dock.remote_server.view.input
    orig = inp.key

    def key(down: bool, keysym: int) -> None:
        if down:
            ch = remote.keysym_char(keysym)
            keys.append(ch if ch and ch.isprintable() else f"{keysym:#x}")
        orig(down, keysym)

    inp.key = key
    return keys


def dock_button_pos(dock: td.Dock, button) -> tuple[int, int]:
    """ドックの帯のボタンの真ん中の、スマホの画面 (CSS の px) での位置。縮めて見せているときはその倍率を掛ける。"""
    x, y, w, h = td.rect_of(tk_get(dock, button.winfo_id))
    ox, oy = remote.client_origin(dock.dock_hwnd)
    s = dock.remote_server.view.scale
    return round((x - ox + w // 2) * s), round((y - oy + h // 2) * s)


def start_edit_app(tag: str) -> tuple[subprocess.Popen, int]:
    """Tk でない試験用の窓 (experiments/edit_testapp.py。上の帯を押すとメニュー、その「検索...」で小窓)。"""
    title = f"edit-testapp {tag}"
    proc = subprocess.Popen([sys.executable, str(Path(__file__).resolve().parent / "experiments" / "edit_testapp.py"), tag])
    for _ in range(100):
        hwnd = td.user32.FindWindowW("EditTestApp", title)
        if hwnd:
            return proc, hwnd
        time.sleep(0.1)
    raise RuntimeError(f"{title} の窓が出ない")


def canvas_dark(tab, x: int, y: int, w: int, h: int) -> int:
    """noVNC の画面 (canvas) の、その範囲の暗い画素の数。メニューの文字が写っているかを見る。"""
    return tab.js(f"(() => {{ const d = document.querySelector('canvas').getContext('2d').getImageData({x}, {y}, {w}, {h})"
                  ".data; let n = 0; for (let i = 0; i < d.length; i += 4) if (d[i] + d[i + 1] + d[i + 2] < 300) n++;"
                  " return n; })()")


def menu_steps(dock: td.Dock, tab, he: int) -> None:
    """Mery のメニューの帯と同じく、押した時にメニューを出す窓。メニューは何度開いても毎回スマホに写るか
    (Mery で「上手く表示されないことが多い」と報告があった)。「検索...」の小窓はスマホの画面の中に出るか"""
    bx, by, bw, bh = td.rect_of(dock.tabs[2].button_hwnd)
    ox, oy = remote.client_origin(dock.dock_hwnd)
    view = dock.remote_server.view
    s = view.scale  # 前のツール (最小の幅) で縮めている
    tab.tap(round((bx - ox + bw // 2) * s), round((by - oy + bh // 2) * s))
    time.sleep(2.0)
    ck.check("Tk でないツール (メニューの帯のある窓) のタブに切り替わる", dock.active is dock.tabs[2],
             dock.active.name if dock.active else "-")
    _, tx, ty, _, _ = td.RemoteSource(dock).layers()[1]
    s = view.scale
    tid = td.user32.GetWindowThreadProcessId(he, None)

    def open_menu() -> tuple | None:
        tab.tap(round((tx + 20) * s), round((ty + 10) * s))
        for _ in range(40):
            layer = next((lr for lr in view.layers()[2:] if remote.Menus.find(he) and lr[0] in view.menus.placed), None)
            if layer:
                return layer
            time.sleep(0.05)
        return None

    shown = []
    for i in range(5):
        layer = open_menu()
        time.sleep(0.6)
        dark = canvas_dark(tab, *(round(v * s) for v in layer[1:])) if layer else -1
        shown.append(dark)
        if i == 0:
            va.screenshot("dock-5-menu")
        tab.tap(round((tx + 200) * s), round((ty + 300) * s))  # 外を押して閉じる
        time.sleep(0.6)
    ck.check("メニューを 5 回開いて、5 回ともスマホの画面に写る (メニューの範囲の暗い画素の数)",
             all(n > 30 for n in shown) and not remote.Menus.find(he), f"{shown} スレッド {tid}")

    layer = open_menu()
    if layer:
        menu = layer[0]
        hmenu = remote.send_result(menu, remote.MN_GETHMENU)
        r = ctypes.wintypes.RECT()
        ctypes.windll.user32.GetMenuItemRect(None, ctypes.c_void_p(hmenu), 2, ctypes.byref(r))  # 「検索...」
        tab.tap(round(((r.left + r.right) // 2 - ox) * s), round(((r.top + r.bottom) // 2 - oy) * s))
    dlg = 0
    for _ in range(60):
        dlg = td.user32.FindWindowW("EditTestFind", "edit-testapp-find")
        if dlg and td.user32.IsWindowVisible(dlg):
            break
        time.sleep(0.05)
    time.sleep(1.0)
    va.screenshot("dock-6-find")
    w, h = remote.client_size(dock.dock_hwnd)
    lr = next((x for x in view.layers() if dlg and x[0] == dlg), None)
    ck.check("ツールが PC の画面の中に収めて出した小窓 (Mery の検索など) が、スマホの画面の中に出る",
             bool(lr) and 0 <= lr[1] and lr[1] + lr[3] <= w and 0 <= lr[2] and lr[2] + lr[4] <= h,
             f"小窓 {lr} ドック {w}x{h} 画面での位置 {dlg and td.rect_of(dlg)}")


def steps(dock: td.Dock) -> None:
    app1, h1 = ck.start_app(f"an{os.getpid()}a")
    app2, h2 = ck.start_app(f"an{os.getpid()}b", 600)
    edit, he = start_edit_app(f"an{os.getpid()}e")
    rotation = (va.adb("shell", "settings", "get", "system", "accelerometer_rotation"),
                va.adb("shell", "settings", "get", "system", "user_rotation"))
    tab_info = tab = None
    try:
        dock.call_in_tk(lambda: dock.adopt("app1", h1))
        dock.call_in_tk(lambda: dock.adopt("app2", h2))
        dock.call_in_tk(lambda: dock.adopt("edit", he))
        dock.call_in_tk(lambda: dock.select(dock.tabs[0]))
        dock.call_in_tk(dock.start_remote)
        for _ in range(100):
            if dock.remote_server:
                break
            time.sleep(0.1)
        url = dock.remote_server.url
        if not LAN:
            va.adb("reverse", f"tcp:{remote.DEFAULT_PORT}", f"tcp:{remote.DEFAULT_PORT}")
        va.adb("forward", f"tcp:{va.CDP_PORT}", "localabstract:chrome_devtools_remote")
        va.adb("shell", "input", "keyevent", "KEYCODE_WAKEUP")
        old = va.tab_ids()
        va.adb("shell", "am", "start", "-a", "android.intent.action.VIEW", "-p", "com.android.chrome", "-d", f"'{url}'")
        tab_info = va.find_tab(30, LAN or "127.0.0.1", old)
        tab = va.Tab(tab_info["webSocketDebuggerUrl"])
        # つながるまで待つ (決め打ちの 4 秒では足りないことがあった)。前に開いたまま古い合言葉でつなぎ直し続けているタブが
        # スマホにあると、Chrome が同じ相手への接続を待たせ、20 秒以上かかった
        t0 = time.monotonic()
        while not dock.remote_server.peers() and time.monotonic() - t0 < 60:
            time.sleep(0.5)
        print(f"つながるまで {time.monotonic() - t0:.1f} 秒")
        time.sleep(3)

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
        keys = record_keys(dock)
        tab.call("Input.insertText", text="abc")
        # 「abc」の直後 (0 秒) に変換を始めると、noVNC 自身が最初の「に」を 2 回送ってきた (人の手では間があく)
        time.sleep(0.5)
        # 日本語の入力と同じく、変換中の文字を出してから確定する。noVNC は変換中の文字を送り、確定で BackSpace で消してから
        # 確定の文字を送ってくる (Mery で順番が崩れ、「にほ」が残ったことがある)
        for comp in ("に", "にほ", "にほん", "にほんご"):
            tab.call("Input.imeSetComposition", text=comp, selectionStart=len(comp), selectionEnd=len(comp))
            time.sleep(0.1)
        tab.call("Input.insertText", text="日本語")
        title = ck.wait_title(h1, "text=abc日本語 ")
        ck.check("文字と、日本語の変換を経た確定文字を打てる", "text=abc日本語 " in title,
                 f"{title} / 届いたキー {' '.join(keys)}")
        tab.call("Input.insertText", text="🍦")  # 絵文字で接続が切れたことがある
        title = ck.wait_title(h1, "text=abc日本語🍦 ")
        time.sleep(1.0)
        alive = bool(dock.remote_server and dock.remote_server.peers())
        ck.check("絵文字を打てて、接続が切れない", "text=abc日本語🍦 " in title and alive, f"{title} 接続 {alive}")
        # カタカナと「ー」「。」は、noVNC が X の古いカナの keysym で送ってくる。文字に戻せず、カタカナに変換すると消えた
        for comp in ("に", "にほ", "にほん"):
            tab.call("Input.imeSetComposition", text=comp, selectionStart=len(comp), selectionEnd=len(comp))
            time.sleep(0.1)
        tab.call("Input.insertText", text="ニホンー。")
        title = ck.wait_title(h1, "text=abc日本語🍦ニホンー。 ")
        ck.check("カタカナに変換した字と「ー」「。」を打てる", "text=abc日本語🍦ニホンー。 " in title,
                 f"{title} / 届いたキー {' '.join(keys[-12:])}")
        va.screenshot("dock-2-typed")
        tab.js("document.getElementById('noVNC_keyboardinput').blur()")
        time.sleep(1.0)

        # 表示倍率: 帯の「− 縮小」を 2 回 (100 → 90 → 75%)、指でタップする
        for _ in range(2):
            tab.tap(*dock_button_pos(dock, dock.zoom_out_button))
            time.sleep(1.5)
        size = remote.client_size(dock.dock_hwnd)
        want = (round(vw / 0.75), round(vh / 0.75))
        time.sleep(1.5)  # 画面の外で広げた所が描かれるのを待ってから撮る
        va.screenshot("dock-2b-zoom75")
        ck.check("「− 縮小」のタップで 75% になり、ドックが 1/0.75 の大きさになる", dock.remote_zoom == 0.75 and size == want,
                 f"倍率 {dock.remote_zoom} ドック {size} / {want}")
        _, tx, ty, _, _ = td.RemoteSource(dock).layers()[1]
        s = dock.remote_server.view.scale
        tab.tap(round((tx + 140) * s), round((ty + 110) * s))
        title = ck.wait_title(h1, "count=3")
        ck.check("縮小した画面の上でも、タップでツールのボタンを押せる", "count=3" in title, title)
        for _ in range(2):
            tab.tap(*dock_button_pos(dock, dock.zoom_in_button))
            time.sleep(1.5)
        size = remote.client_size(dock.dock_hwnd)
        ck.check("「+ 拡大」で 100% に戻る", dock.remote_zoom == 1.0 and size == (vw, vh), f"倍率 {dock.remote_zoom} ドック {size}")

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
        menu_steps(dock, tab, he)

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
        if LAN:
            peers = sorted({ln.split(" ")[2].rstrip(":") for ln in log.splitlines() if ": つながった" in ln})
            ck.check("スマホは Wi-Fi のアドレスからつないできた (USB を通っていない)", peers and "127.0.0.1" not in peers,
                     f"つないできたアドレス {peers}")
    except (Exception, SystemExit) as e:  # SystemExit (タブが見つからない など) もここで受ける
        traceback.print_exc()
        ck.check("途中で止まらない", False, str(e))
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
        if not LAN:
            va.adb("reverse", "--remove", f"tcp:{remote.DEFAULT_PORT}", check=False)
        # 試験用の窓を先に閉じてから、ドックを閉じる (ドックを閉じるとこのプロセスが終わり、このスレッドも止まる)
        ck.close_apps([(app1, h1), (app2, h2), (edit, he)])
        dock.call_in_tk(dock.stop_remote)
        dock.call_in_tk(dock.quit, wait=False)


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    OUT.mkdir(exist_ok=True)
    if va.adb("get-state", check=False) != "device":
        raise SystemExit("実機がつながっていない、または USB デバッグが未許可")
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
    print("つなぎ方:", f"Wi-Fi ({LAN})" if LAN else "USB (adb reverse)")
    dock = td.Dock([], {"auto_adopt": False, "restore_session": False, "remote": {"host": LAN or "127.0.0.1"}})
    dock.root.geometry("900x600+150+120")
    threading.Thread(target=lambda: (time.sleep(1.0), steps(dock)), daemon=True).start()
    dock.root.mainloop()
    ng = [r for r in ck.results if not r[1]]
    print(f"\n{len(ck.results) - len(ng)}/{len(ck.results)} OK")
    sys.exit(1 if ng else 0)


if __name__ == "__main__":
    main()
