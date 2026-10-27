# /// script
# requires-python = ">=3.11"
# dependencies = ["pillow", "numpy", "websocket-client"]
# ///
"""遠隔 (スマホのブラウザから使う。remote.py) を、ドックを開いて確かめる。

    uv run check_remote.py

試験用の窓 (experiments/vnc_testapp.py) を 2 つ取り込み (1 つは最小の幅 600)、noVNC と同じ WebSocket でつなぐ。
- 合言葉 (token) と Origin が合わない接続は断る。トップのページ (/) は見せない
- つなぐと、ドックが画面の外に出て、頼んだ大きさになる。切ると元の大きさ・位置に戻る
- タップ・長押し・日本語の入力が、選んでいるツールに届く。タブを押すと切り替わる。「+ 起動」は押されない
- 最小の幅のツールは、縮めて収める
人の設定・前回のタブには触れない (控えの置き場所 WINTABDOCK_HOME を一時フォルダにする)。ポート 15900 を使う。
noVNC を開いたブラウザのタブは閉じておく (自動でつなぎ直して大きさを頼んでくる)。
"""

import ctypes
import json
import os
import socket
import struct
import sys
import tempfile
import threading
import time
import traceback
import zlib
from pathlib import Path

HOME = Path(tempfile.mkdtemp(prefix="wintabdock-remote-"))
os.environ["WINTABDOCK_HOME"] = str(HOME)  # wintabdock を読み込む前に (控えの置き場所はそこで決まる)
NOVNC_CACHE = Path(os.environ.get("LOCALAPPDATA", ".")) / "WinTabDock"
if (NOVNC_CACHE / "novnc-1.6.0").exists():  # 毎回ダウンロードしないように、人の控えにある noVNC を写す
    import shutil
    shutil.copytree(NOVNC_CACHE / "novnc-1.6.0", HOME / "novnc-1.6.0")

import websocket  # noqa: E402
from PIL import Image  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent / "experiments"))
import remote  # noqa: E402
import vnc_probe_check as ck  # noqa: E402  (試験用の窓を起動する手順を借りる)
import wintabdock as td  # noqa: E402

OUT = Path(__file__).resolve().parent / "experiments" / "out"
PORT = remote.DEFAULT_PORT
results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(("OK  " if ok else "NG  ") + name + (f"  ({detail})" if detail else ""), flush=True)


def ws_status(path: str, origin: str, host: str = f"127.0.0.1:{PORT}") -> int:
    with socket.create_connection(("127.0.0.1", PORT), timeout=5) as s:
        s.sendall(f"GET {path} HTTP/1.1\r\nHost: {host}\r\nOrigin: {origin}\r\nUpgrade: websocket\r\n"
                  "Connection: Upgrade\r\nSec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\nSec-WebSocket-Version: 13\r\n"
                  "Sec-WebSocket-Protocol: binary\r\n\r\n".encode())
        return int(s.recv(64).split(b" ")[1])


def http_status(path: str) -> int:
    with socket.create_connection(("127.0.0.1", PORT), timeout=5) as s:
        s.sendall(f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{PORT}\r\n\r\n".encode())
        return int(s.recv(64).split(b" ")[1])


class Client:
    """noVNC と同じく WebSocket でつなぐ最小の VNC クライアント (Zlib と Raw を読む)。"""

    def __init__(self, token: str):
        self.ws = websocket.create_connection(f"ws://127.0.0.1:{PORT}/websockify?token={token}",
                                              origin=f"http://127.0.0.1:{PORT}", subprotocols=["binary"], timeout=10)
        self.buf = bytearray()
        self.inflate = zlib.decompressobj()
        assert self.recv(12) == b"RFB 003.008\n"
        self.send(b"RFB 003.008\n")
        self.recv(self.recv(1)[0])
        self.send(b"\x01")
        assert struct.unpack(">I", self.recv(4))[0] == 0
        self.send(b"\x01")
        w, h = struct.unpack(">HH", self.recv(4))
        self.recv(16)
        self.recv(struct.unpack(">I", self.recv(4))[0])
        self.fb = Image.new("RGB", (w, h))
        # noVNC と同じ画素の形 (32 ビット、赤が下位) を頼む
        self.send(struct.pack(">BxxxBBBBHHHBBBxxx", 0, 32, 24, 0, 1, 255, 255, 255, 0, 8, 16))
        encs = [remote.ENC_ZLIB, remote.ENC_RAW, remote.PSEUDO_EXT_DESKTOP_SIZE, remote.PSEUDO_DESKTOP_SIZE]
        self.send(struct.pack(f">BxH{len(encs)}i", 2, len(encs), *encs))
        self.size_msgs: list[tuple[int, int, int, int]] = []

    def send(self, data: bytes) -> None:
        self.ws.send_binary(data)

    def recv(self, n: int) -> bytes:
        while len(self.buf) < n:
            self.buf += self.ws.recv()
        out = bytes(self.buf[:n])
        del self.buf[:n]
        return out

    def read_update(self) -> bool:
        assert self.recv(1)[0] == 0
        n = struct.unpack(">xH", self.recv(3))[0]
        pixels = False
        for _ in range(n):
            x, y, w, h, enc = struct.unpack(">HHHHi", self.recv(12))
            if enc == remote.PSEUDO_EXT_DESKTOP_SIZE:
                self.recv(16 * self.recv(4)[0])
                self.size_msgs.append((x, y, w, h))
                if x != 1 or y == 0:
                    self.fb = Image.new("RGB", (w, h))
            elif enc in (remote.ENC_ZLIB, remote.ENC_RAW):
                data = self.recv(w * h * 4) if enc == remote.ENC_RAW else \
                    self.inflate.decompress(self.recv(struct.unpack(">I", self.recv(4))[0]))
                self.fb.paste(Image.frombytes("RGB", (w, h), data, "raw", "RGBX"), (x, y))
                pixels = True
        return pixels

    def full_frame(self) -> Image.Image:
        # 前に頼んだ分の画像が届く途中のことがある (それは古い)。頼んで読むのを 2 回して、2 枚目を使う
        for _ in range(2):
            self.send(struct.pack(">BBHHHH", 3, 0, 0, 0, *self.fb.size))
            while not self.read_update():
                self.send(struct.pack(">BBHHHH", 3, 0, 0, 0, *self.fb.size))
            time.sleep(0.3)
        return self.fb.copy()

    def set_size(self, w: int, h: int) -> tuple[int, int, int, int]:
        self.send(struct.pack(">BxHHBx", 251, w, h, 1) + struct.pack(">IHHHHI", 0, 0, 0, w, h, 0))
        before = len(self.size_msgs)
        while not any(m[0] == 1 for m in self.size_msgs[before:]):
            self.send(struct.pack(">BBHHHH", 3, 1, 0, 0, *self.fb.size))
            self.read_update()
        return next(m for m in self.size_msgs[before:] if m[0] == 1)

    def tap(self, x: int, y: int, hold: float = 0.08) -> None:
        self.send(struct.pack(">BBHH", 5, 0, x, y))
        time.sleep(0.05)
        self.send(struct.pack(">BBHH", 5, 1, x, y))
        time.sleep(hold)
        self.send(struct.pack(">BBHH", 5, 0, x, y))

    def key(self, keysym: int) -> None:
        for down in (1, 0):
            self.send(struct.pack(">BB2xI", 4, down, keysym))

    def close(self) -> None:
        self.ws.close()


def steps(dock: td.Dock) -> None:
    app1, h1 = ck.start_app(f"r{os.getpid()}a")
    app2, h2 = ck.start_app(f"r{os.getpid()}b", 600)
    c = None
    try:
        dock.call_in_tk(lambda: dock.adopt("app1", h1))
        dock.call_in_tk(lambda: dock.adopt("app2", h2))
        dock.call_in_tk(lambda: dock.select(dock.tabs[0]))
        original = tk_get(dock, dock.root.geometry)
        dock.call_in_tk(dock.start_remote)
        for _ in range(100):
            if dock.remote_server:
                break
            time.sleep(0.1)
        check("待ち受けが始まる", bool(dock.remote_server), str(HOME / "remote.log"))
        token = dock.prefs["remote_token"]
        good_origin = f"http://127.0.0.1:{PORT}"
        check("トップのページ (/) は見せない", http_status("/") == 404)
        check("noVNC のページは配る", http_status("/vnc.html") == 200)
        bad = ["/\\\\127.0.0.1\\c$\\x", "/C:/Windows/win.ini", "/../remote.log", "/app/../../remote.log", "/%5C%5Cx"]
        check("noVNC のフォルダの外 (\\\\相手\\共有・C:・..) は配らない (ファイルに触れる前に断る)",
              all(http_status(p) == 404 for p in bad)
              and not any(remote._SAFE_PATH.fullmatch(p.lstrip("/")) and ".." not in p.split("/") for p in bad[:2]),
              ", ".join(f"{p}={http_status(p)}" for p in bad))
        check("合言葉が無いと断る", ws_status("/websockify", good_origin) == 403)
        check("合言葉が違うと断る", ws_status("/websockify?token=wrong", good_origin) == 403)
        check("よそのサイト (Origin) からは断る", ws_status(f"/websockify?token={token}", "http://evil.example") == 403)
        check("合言葉と Origin が合えば受ける", ws_status(f"/websockify?token={token}", good_origin) == 101)
        check("URL に合言葉が入っている", token in dock.remote_server.url and "resize=remote" in dock.remote_server.url,
              dock.remote_server.url.replace(token, "<合言葉>"))
        time.sleep(1.0)

        c = Client(token)
        c.full_frame()
        time.sleep(0.5)
        r = td.rect_of(dock.dock_hwnd)
        check("つなぐと、ドックが画面の外に出る", r[0] >= td.user32.GetSystemMetrics(78), f"left={r[0]}")
        reply = c.set_size(411, 772)
        time.sleep(0.5)
        img = c.full_frame()
        img.save(OUT / "remote-1-phone.png")
        check("411x772 を頼むと、ドックの内側がその大きさになる",
              reply == (1, 0, 411, 772) and remote.client_size(dock.dock_hwnd) == (411, 772),
              f"返事 {reply} ドック {remote.client_size(dock.dock_hwnd)}")
        layers = RemoteSource_layers(dock)
        _, tx, ty, tw, th = layers[1]
        tool_img = img.crop((tx, ty, tx + tw, ty + th))
        check("選んでいるツールが、タブの帯の下に写る (四隅)", not ck.corners_ok(tool_img),
              f"ツール {tx},{ty} {tw}x{th} {ck.corners_ok(tool_img)}")

        c.tap(tx + 140, ty + 110)
        title = ck.wait_title(h1, "count=1")
        check("タップでツールのボタンを押せる", "count=1" in title, title)
        c.tap(tx + 140, ty + 110, hold=0.6)
        title = ck.wait_title(h1, "count=2")
        check("長押し (0.6 秒) でも押せる", "count=2" in title, title)
        c.tap(tx + 210, ty + 190)
        time.sleep(0.3)
        for ks in (ord("o"), ord("k"), 0x01003042, 0x01003044, 0xFF08):  # o k あ い BackSpace
            c.key(ks)
        title = ck.wait_title(h1, "text=okあ ")
        check("文字と日本語を打てる", "text=okあ " in title, title)

        menu_x, menu_y = button_pos(dock, tk_get(dock, dock.menu_button.winfo_id))
        menus_before = len(our_menus())
        c.tap(menu_x, menu_y)
        time.sleep(0.8)
        t0 = time.monotonic()
        dock.call_in_tk(lambda: None)
        stuck = time.monotonic() - t0
        menus = len(our_menus())
        check("「+ 起動」は遠隔から押されない (メニューが出ず、ドックが止まらない)", menus == menus_before and stuck < 1,
              f"メニュー {menus_before} → {menus} 個、応答 {stuck:.2f} 秒")

        tab2_x, tab2_y = button_pos(dock, dock.tabs[1].button_hwnd)
        c.tap(tab2_x, tab2_y)
        time.sleep(1.0)
        check("タブを押すと切り替わる", dock.active is dock.tabs[1], dock.active.name if dock.active else "-")
        # 切り替えて縦に伸ばした直後は、ツールがまだ描き終えておらず一部が黒いことがある (次の画像で正しく届く)。
        # 3 秒以内に正しく写れば良い
        deadline = time.monotonic() + 3
        while True:
            img = c.full_frame()
            layers = RemoteSource_layers(dock)
            _, tx, ty, tw, th = layers[1]
            s = 411 / (tx + tw)
            fit = img.crop((round(tx * s), round(ty * s), round((tx + tw) * s), round((ty + th) * s)))
            if not ck.corners_ok(fit) or time.monotonic() > deadline:
                break
        img.save(OUT / "remote-2-minwidth.png")
        # 切り替えた直後 (0.2 秒以内) のタップは、30 回に 1 回ほど抜けた (窓を出して大きさを変えた直後)。少し待ってから押す
        time.sleep(0.8)
        # Tk の最小の幅は枠を含めた窓の大きさに効くので、枠を外したツールの内側は 600 より少し広い (616 だった)
        check("最小の幅 600 のツールは、縮めて収まる (四隅)", tw >= 600 and img.size == (411, 772) and not ck.corners_ok(fit),
              f"ツール {tw}x{th} 画像 {img.size} {ck.corners_ok(fit)}")
        c.tap(round((tx + 140) * s), round((ty + 110) * s))
        title = ck.wait_title(h2, "count=1")
        check("縮めた画面の上でも押せる", "count=1" in title, title)

        c.close()
        c = None
        time.sleep(1.5)
        now = tk_get(dock, dock.root.geometry)
        check("切ると、ドックが元の大きさ・位置に戻る", now == original, f"{now} / 元 {original}")
        dock.call_in_tk(dock.save_session)
        saved = json.loads((HOME / "session.json").read_text(encoding="utf-8"))["geometry"]
        check("前回の位置には、元の位置を覚える", saved == original, saved)
    except Exception:
        traceback.print_exc()
        check("途中で止まらない", False)
    finally:
        if c:
            c.close()
        # 試験用の窓を先に閉じてから、ドックを閉じる。ドックを閉じるとこのプロセスが終わり、このスレッドも止まる
        # (先にドックを閉じていたら、窓を閉じる前に止まり、試験用の窓が 90 個残った)
        ck.close_apps([(app1, h1), (app2, h2)])
        dock.call_in_tk(dock.stop_remote)
        dock.call_in_tk(dock.quit, wait=False)


def tk_get(dock: td.Dock, fn):
    """Tk のスレッドで fn を動かして値を受け取る (試験のスレッドから Tk に直接触らない)。"""
    box = []
    dock.call_in_tk(lambda: box.append(fn()))
    if not box:  # Tk のスレッドが応えない。どのスレッドがどこにいるかを書き出す
        import faulthandler
        faulthandler.dump_traceback(all_threads=True)
    return box[0] if box else None


def RemoteSource_layers(dock: td.Dock) -> list:  # noqa: N802
    return td.RemoteSource(dock).layers()


def button_pos(dock: td.Dock, hwnd: int) -> tuple[int, int]:
    """ドックの中のボタンの真ん中 (ドックの内側の座標 = 遠隔の画面の座標。縮めていないとき)。"""
    x, y, w, h = td.rect_of(hwnd)
    ox, oy = remote.client_origin(dock.dock_hwnd)
    s = remote.client_size(dock.dock_hwnd)[0] and dock.remote_server.view.scale if dock.remote_server else 1
    return round((x - ox + w // 2) * s), round((y - oy + h // 2) * s)


def watch_tk(dock: td.Dock) -> None:
    """Tk のスレッドが 1.5 秒以上応えなくなったら、その瞬間のほかのスレッドの居場所を一度だけ書き出す。"""
    import faulthandler
    last = [time.monotonic()]

    def tick():
        last[0] = time.monotonic()
        dock.root.after(20, tick)

    def watch():
        while True:
            time.sleep(0.2)
            if time.monotonic() - last[0] > 1.5:
                # Tk のスレッドが Windows の知らせを処理できるか (できるなら、止まっているのではなく
                # Tk の時計 (after) が来ないだけ)
                res = ctypes.c_size_t()
                t0 = time.monotonic()
                ok = remote._u.SendMessageTimeoutW(dock.dock_hwnd, 0, 0, 0, remote.SMTO_ABORTIFHUNG, 1000,
                                                   ctypes.byref(res))
                hung = ctypes.windll.user32.IsHungAppWindow(dock.dock_hwnd)
                print(f"--- Tk が応えない ({time.strftime('%H:%M:%S')}) WM_NULL={'届いた' if ok else '届かない'}"
                      f" {time.monotonic() - t0:.2f} 秒, IsHungAppWindow={hung}", flush=True)
                # 別のスレッドから Tcl の命令を呼ぶ (tkinter は Tk のスレッドに頼んで待つ)。戻れば Tcl の出来事は
                # 処理されていて、時計 (after) だけが来ない
                box = []
                threading.Thread(target=lambda: box.append(dock.root.tk.call("info", "patchlevel")), daemon=True).start()
                t0 = time.monotonic()
                while not box and time.monotonic() - t0 < 3:
                    time.sleep(0.05)
                print(f"    Tcl の命令: {'戻った ' + str(box[0]) if box else '3 秒戻らない'} {time.monotonic() - t0:.2f} 秒",
                      flush=True)
                # ドックの窓に知らせを 1 つ置く (PostMessage)。GetMessage で待っている Tk が目を覚まし、
                # 溜まっている時計 (after) を処理するか
                stalled_since = last[0]
                ctypes.windll.user32.PostMessageW(dock.dock_hwnd, 0, 0, 0)
                time.sleep(0.5)
                print(f"    知らせを置いたあと: Tk の時計は{'動き出した' if last[0] > stalled_since else 'まだ止まっている'}",
                      flush=True)
                if os.environ.get("CHECK_REMOTE_PYSPY"):  # Tk のスレッドの中を C の関数まで書き出す
                    import subprocess
                    r = subprocess.run(["uvx", "py-spy", "dump", "--native", "--pid", str(os.getpid())],
                                       capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
                    print("    py-spy:\n" + (r.stdout or r.stderr), flush=True)
                for t in list(dock.tabs):  # ツールの窓は応えるか (Tk のスレッドがツールの返事を待っていないか)
                    t0 = time.monotonic()
                    ok = remote._u.SendMessageTimeoutW(t.hwnd, 0, 0, 0, remote.SMTO_ABORTIFHUNG, 1000, ctypes.byref(res))
                    print(f"    ツール {t.name}: WM_NULL={'届いた' if ok else '届かない'} {time.monotonic() - t0:.2f} 秒,"
                          f" IsHungAppWindow={ctypes.windll.user32.IsHungAppWindow(t.hwnd)}", flush=True)
                faulthandler.dump_traceback(all_threads=True)
                return

    dock.root.after(20, tick)
    threading.Thread(target=watch, daemon=True).start()


def child_windows(hwnd: int) -> list[int]:
    found: list[int] = []
    ctypes.windll.user32.EnumChildWindows(hwnd, td.WNDENUMPROC(lambda h, _: found.append(h) or True), 0)
    return found


def our_menus() -> list[int]:
    """このプロセスの、見えているメニューの窓。"""
    return [h for h in all_windows() if window_class(h) == "#32768" and td.user32.IsWindowVisible(h)
            and td.window_pid(h) == os.getpid()]


def all_windows() -> list[int]:
    found: list[int] = []
    td.user32.EnumWindows(td.WNDENUMPROC(lambda h, _: found.append(h) or True), 0)
    return found


def window_class(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(64)
    ctypes.windll.user32.GetClassNameW(hwnd, buf, 64)
    return buf.value


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    OUT.mkdir(exist_ok=True)
    try:
        socket.create_connection(("127.0.0.1", PORT), timeout=1).close()
        raise SystemExit(f"ポート {PORT} が使われている (vnc_probe.py やドックの遠隔を止めてから流す)")
    except OSError:
        pass
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
    if os.environ.get("CHECK_REMOTE_DUMP"):  # 止まったときに、どのスレッドがどこにいるかを書き出す
        import faulthandler
        faulthandler.dump_traceback_later(int(os.environ["CHECK_REMOTE_DUMP"]), exit=True)
    dock = td.Dock([], {"auto_adopt": False, "restore_session": False, "remote": {"host": "127.0.0.1"}})
    dock.root.geometry("900x600+150+120")
    if os.environ.get("CHECK_REMOTE_DUMP"):
        watch_tk(dock)
    threading.Thread(target=lambda: (time.sleep(1.0), steps(dock)), daemon=True).start()
    dock.root.mainloop()
    ng = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(ng)}/{len(results)} OK")
    sys.exit(1 if ng else 0)


if __name__ == "__main__":
    main()
