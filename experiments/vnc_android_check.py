# /// script
# requires-python = ">=3.11"
# dependencies = ["pillow", "numpy", "websocket-client"]
# ///
"""vnc_probe.py を Android の実機の Chrome (noVNC) から確かめる。USB でつなぎ、USB デバッグを許可しておく。

    uv run experiments/vnc_android_check.py

- スマホの 127.0.0.1:15900 を PC に届ける (adb reverse)。Tailscale は使わず、ポートを外に開けない
- スマホの Chrome に noVNC のタブを開き、Chrome の開発用の口 (adb forward tcp:29222) から本物のタッチを送る。
  日本語は、日本語の入力で確定したのと同じ形 (Input.insertText) で noVNC の入力欄に流し込む
- 画面を横向きにして戻す。スマホの回転の設定は控えて、最後に戻す。開いたタブも閉じる
- 試験用の窓 (vnc_testapp.py) だけを相手にする。画面写真は experiments/out/android-*.png
"""

import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import websocket

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import vnc_probe as vp  # noqa: E402
import vnc_probe_check as ck  # noqa: E402

CDP_PORT = 29222  # LPortMan の台帳に「WinTabDock: phone Chrome DevTools (adb forward)」で登録
OUT = HERE / "out"


def adb(*args: str, check: bool = True) -> str:
    r = subprocess.run(["adb", *args], capture_output=True, text=True, encoding="utf-8")
    if check and r.returncode:
        raise SystemExit(f"adb {' '.join(args)}: {r.stderr.strip()}")
    return r.stdout.strip()


class Tab:
    """スマホの Chrome の 1 つのタブを、開発用の口 (CDP) で動かす。"""

    def __init__(self, ws_url: str):
        self.ws = websocket.create_connection(ws_url, timeout=10, suppress_origin=True)
        self.next_id = 0

    def call(self, method: str, **params):
        self.next_id += 1
        self.ws.send(json.dumps({"id": self.next_id, "method": method, "params": params}))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == self.next_id:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error']}")
                return msg.get("result", {})

    def js(self, expr: str):
        return self.call("Runtime.evaluate", expression=expr, returnByValue=True)["result"].get("value")

    def tap(self, x: float, y: float, hold: float = 0.08) -> None:
        pt = [{"x": x, "y": y}]
        self.call("Input.dispatchTouchEvent", type="touchStart", touchPoints=pt)
        time.sleep(hold)
        self.call("Input.dispatchTouchEvent", type="touchEnd", touchPoints=[])

    def close(self) -> None:
        self.ws.close()


def screenshot(name: str) -> None:
    data = subprocess.run(["adb", "exec-out", "screencap", "-p"], capture_output=True).stdout
    (OUT / f"android-{name}.png").write_bytes(data)


def tab_ids() -> set[str]:
    """いまスマホの Chrome に開いているタブ。find_tab で、開く前から在ったタブを除くのに使う。"""
    tabs = json.load(urllib.request.urlopen(f"http://127.0.0.1:{CDP_PORT}/json", timeout=3))
    return {t["id"] for t in tabs}


def find_tab(timeout: float = 20, host: str = "127.0.0.1", old: set[str] = frozenset()) -> dict:
    """noVNC のタブを探す。old (開く前から在ったタブ) は選ばない。
    前の試験で残ったタブは裏でつなぎ直し続けていて、JavaScript には応えるがタッチは返ってこない (2026-10-27)"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            tabs = json.load(urllib.request.urlopen(f"http://127.0.0.1:{CDP_PORT}/json", timeout=3))
            for t in tabs:
                if (t.get("type") == "page" and t["id"] not in old
                        and f"{host}:{vp.PORT}/vnc.html" in t.get("url", "")):
                    return t
        except OSError:
            pass
        time.sleep(0.5)
    raise SystemExit("スマホの Chrome に noVNC のタブが見つからない")


def wait_fb(tab: Tab, want_w: int, want_h: int, timeout: float = 8) -> tuple[int, int]:
    """noVNC の画面の大きさ (サーバから届いたもの) が want になるまで待つ。"""
    deadline = time.monotonic() + timeout
    size = (0, 0)
    while time.monotonic() < deadline:
        c = tab.js("(() => { const c = document.querySelector('#noVNC_container canvas'); "
                   "return c ? [c.width, c.height] : [0, 0]; })()")
        size = tuple(c)
        if size == (want_w, want_h):
            break
        time.sleep(0.3)
    return size


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    OUT.mkdir(exist_ok=True)
    state = adb("get-state", check=False)
    if state != "device":
        raise SystemExit(f"実機がつながっていない、または USB デバッグが未許可 ({state or adb('devices')})")
    print("実機:", adb("shell", "getprop", "ro.product.model"), "Android", adb("shell", "getprop", "ro.build.version.release"))
    rotation = (adb("shell", "settings", "get", "system", "accelerometer_rotation"),
                adb("shell", "settings", "get", "system", "user_rotation"))

    app, hwnd = ck.start_app(f"a{os.getpid()}")
    probe = ck.start_probe(hwnd, "--offscreen", "--verbose", log=OUT / "android-probe.log")
    tab_info = None
    tab = None
    try:
        adb("reverse", f"tcp:{vp.PORT}", f"tcp:{vp.PORT}")
        adb("forward", f"tcp:{CDP_PORT}", "localabstract:chrome_devtools_remote")
        adb("shell", "input", "keyevent", "KEYCODE_WAKEUP")
        old = tab_ids()
        adb("shell", "am", "start", "-a", "android.intent.action.VIEW", "-p", "com.android.chrome",
            "-d", f"http://127.0.0.1:{vp.PORT}/")
        tab_info = find_tab(old=old)
        tab = Tab(tab_info["webSocketDebuggerUrl"])
        time.sleep(3)

        vw, vh, dpr = tab.js("[innerWidth, innerHeight, devicePixelRatio]")
        print(f"縦向き: ブラウザの中は {vw}x{vh} (CSS の px)、devicePixelRatio {dpr}")
        fb = wait_fb(tab, vw, vh)
        screenshot("1-portrait")
        ck.check("縦向き: noVNC の画面がブラウザの中の大きさになる", fb == (vw, vh), f"{fb} / {vw}x{vh}")
        ck.check("縦向き: 窓の内側もその大きさになる", vp.client_size(hwnd) == (vw, vh), str(vp.client_size(hwnd)))

        tab.tap(140, 110)
        title = ck.wait_title(hwnd, "count=1")
        ck.check("タップでボタンを押せる", "count=1" in title, title)
        tab.tap(140, 110, hold=0.6)
        title = ck.wait_title(hwnd, "count=2")
        ck.check("長押し (0.6 秒) でもボタンを押せる", "count=2" in title, title)

        tab.tap(210, 190)
        time.sleep(0.5)
        # noVNC のスマホ用の入力欄 (画面のキーボードを出すボタンが使うもの) に、確定した文字を流し込む
        tab.js("document.getElementById('noVNC_keyboardinput').focus()")
        time.sleep(0.5)
        tab.call("Input.insertText", text="abc")
        tab.call("Input.insertText", text="日本語")
        title = ck.wait_title(hwnd, "text=abc日本語 ")
        ck.check("文字と日本語の確定文字を打てる", "text=abc日本語 " in title, title)
        screenshot("2-typed")
        tab.js("document.getElementById('noVNC_keyboardinput').blur()")

        adb("shell", "settings", "put", "system", "accelerometer_rotation", "0")
        adb("shell", "settings", "put", "system", "user_rotation", "1")
        time.sleep(3)
        vw2, vh2 = tab.js("[innerWidth, innerHeight]")
        print(f"横向き: ブラウザの中は {vw2}x{vh2}")
        fb = wait_fb(tab, vw2, vh2)
        screenshot("3-landscape")
        ck.check("横向き: noVNC の画面と窓が、横長の大きさに変わる",
                 vw2 > vh2 and fb == (vw2, vh2) and vp.client_size(hwnd) == (vw2, vh2),
                 f"noVNC {fb} 窓 {vp.client_size(hwnd)} / {vw2}x{vh2}")
        tab.tap(140, 110)
        title = ck.wait_title(hwnd, "count=3")
        ck.check("横向き: タップでボタンを押せる", "count=3" in title, title)
        log = (OUT / "android-probe.log").read_text(encoding="utf-8")
        n = log.count("大きさの要求") + log.count("同じ大きさなので変えない")
        ck.check("大きさの要求が繰り返されない (縦と横で 6 回まで)", n <= 6, f"{n} 回")
    finally:
        adb("shell", "settings", "put", "system", "accelerometer_rotation", rotation[0], check=False)
        adb("shell", "settings", "put", "system", "user_rotation", rotation[1], check=False)
        if tab:
            tab.close()
        if tab_info:
            urllib.request.urlopen(f"http://127.0.0.1:{CDP_PORT}/json/close/{tab_info['id']}", timeout=5).read()
        adb("forward", "--remove", f"tcp:{CDP_PORT}", check=False)
        adb("reverse", "--remove", f"tcp:{vp.PORT}", check=False)
        ck.stop_probe(probe)
        vp.user32.PostMessageW(hwnd, vp.WM_CLOSE, 0, 0)
        app.wait(10)
    ng = [r for r in ck.results if not r[1]]
    print(f"\n{len(ck.results) - len(ng)}/{len(ck.results)} OK")
    sys.exit(1 if ng else 0)


if __name__ == "__main__":
    main()
