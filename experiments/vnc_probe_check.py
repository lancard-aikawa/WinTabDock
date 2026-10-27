# /// script
# requires-python = ">=3.11"
# dependencies = ["pillow", "numpy"]
# ///
"""vnc_probe.py の確かめ方。試験用の窓 (vnc_testapp.py) を起動し、VNC アプリのふりをしてつなぐ。

    uv run experiments/vnc_probe_check.py

人のツールには触れない (試験用の窓だけを相手にする)。ポート 15900 を使うので、vnc_probe.py を止めてから流す。
noVNC を開いたブラウザのタブも閉じておく。自動でつなぎ直し、自分の大きさを要求して試験の窓を変えてしまう。
画像は experiments/out/ に書く。
"""

import ctypes
import io
import os
import socket
import struct
import subprocess
import sys
import time
from ctypes import wintypes
from pathlib import Path

from PIL import Image

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import vnc_probe as vp  # noqa: E402

OUT = HERE / "out"
results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(("OK  " if ok else "NG  ") + name + (f"  ({detail})" if detail else ""), flush=True)


class Client:
    """最小の VNC クライアント (Tight の JPEG と Raw だけ読む)。"""

    def __init__(self, port: int):
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=10)
        self.recv = vp.TcpStream(self.sock).recv
        assert self.recv(12) == b"RFB 003.008\n"
        self.sock.sendall(b"RFB 003.008\n")
        n = self.recv(1)[0]
        self.recv(n)
        self.sock.sendall(b"\x01")
        assert struct.unpack(">I", self.recv(4))[0] == 0
        self.sock.sendall(b"\x01")
        w, h = struct.unpack(">HH", self.recv(4))
        self.recv(16)
        self.recv(struct.unpack(">I", self.recv(4))[0])
        self.fb = Image.new("RGB", (w, h))
        encs = [vp.ENC_TIGHT, vp.ENC_RAW, vp.PSEUDO_EXT_DESKTOP_SIZE, vp.PSEUDO_DESKTOP_SIZE, -26]
        self.sock.sendall(struct.pack(f">BxH{len(encs)}i", 2, len(encs), *encs))
        self.size_msgs: list[tuple[int, int, int, int]] = []  # (理由, 状態, 幅, 高さ)

    def request(self, incremental: bool = False) -> None:
        w, h = self.fb.size
        self.sock.sendall(struct.pack(">BBHHHH", 3, incremental, 0, 0, w, h))

    def read_update(self) -> bool:
        """更新を 1 つ読む。画素が入っていれば True (大きさの知らせだけなら False)。"""
        t = self.recv(1)[0]
        assert t == 0, t
        n = struct.unpack(">xH", self.recv(3))[0]
        pixels = False
        for _ in range(n):
            x, y, w, h, enc = struct.unpack(">HHHHi", self.recv(12))
            if enc == vp.PSEUDO_EXT_DESKTOP_SIZE:
                screens = self.recv(4)[0]
                self.recv(16 * screens)
                self.size_msgs.append((x, y, w, h))
                self.fb = Image.new("RGB", (w, h))
            elif enc == vp.PSEUDO_DESKTOP_SIZE:
                self.size_msgs.append((0, 0, w, h))
                self.fb = Image.new("RGB", (w, h))
            elif enc == vp.ENC_TIGHT:
                assert self.recv(1)[0] == 0x90
                length, shift = 0, 0
                for _ in range(3):
                    b = self.recv(1)[0]
                    length |= (b & 0x7F if shift < 14 else b) << shift
                    shift += 7
                    if not b & 0x80:
                        break
                self.fb.paste(Image.open(io.BytesIO(self.recv(length))).convert("RGB"), (x, y))
            elif enc == vp.ENC_RAW:
                self.fb.paste(Image.frombytes("RGB", (w, h), self.recv(w * h * 4), "raw", "RGBX"), (x, y))
            else:
                raise AssertionError(f"知らない符号 {enc}")
            pixels |= enc in (vp.ENC_TIGHT, vp.ENC_RAW)
        return pixels

    def full_frame(self) -> Image.Image:
        self.request(False)
        while not self.read_update():
            self.request(False)
        return self.fb.copy()

    def set_size(self, w: int, h: int) -> tuple[int, int, int, int]:
        self.sock.sendall(struct.pack(">BxHHBx", 251, w, h, 1) + struct.pack(">IHHHHI", 0, 0, 0, w, h, 0))
        before = len(self.size_msgs)
        while not any(m[0] == 1 for m in self.size_msgs[before:]):  # 理由 1 = この要求への返事
            self.request(True)
            self.read_update()
        return next(m for m in self.size_msgs[before:] if m[0] == 1)

    def pointer(self, mask: int, x: int, y: int) -> None:
        self.sock.sendall(struct.pack(">BBHH", 5, mask, x, y))

    def key(self, keysym: int) -> None:
        for down in (1, 0):
            self.sock.sendall(struct.pack(">BB2xI", 4, down, keysym))

    def close(self) -> None:
        self.sock.close()


def corners_ok(img: Image.Image) -> str:
    """四隅の札の色が写っているか。写っていない隅の名前を返す (全部写っていれば空)。"""
    w, h = img.size
    want = {"左上": ((6, 6), (229, 57, 53)), "右上": ((w - 7, 6), (67, 160, 71)),
            "左下": ((6, h - 7), (30, 136, 229)), "右下": ((w - 7, h - 7), (253, 216, 53))}
    bad = []
    for name, (pt, rgb) in want.items():
        got = img.getpixel(pt)
        if max(abs(a - b) for a, b in zip(got, rgb)) > 40:
            bad.append(f"{name}={got}")
    return ", ".join(bad)


def wait_title(hwnd: int, part: str, timeout: float = 3.0) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        t = vp.window_text(hwnd)
        if part in t:
            return t
        time.sleep(0.1)
    return vp.window_text(hwnd)


def start_probe(hwnd: int, *extra: str, log: Path | None = None) -> subprocess.Popen:
    out = open(log, "w", encoding="utf-8") if log else subprocess.DEVNULL  # noqa: SIM115 (vnc_probe が終わるまで開いておく)
    p = subprocess.Popen([sys.executable, "-X", "utf8", str(HERE / "vnc_probe.py"), "--hwnd", str(hwnd), *extra],
                         stdout=out, stderr=subprocess.STDOUT if log else subprocess.DEVNULL,
                         creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            socket.create_connection(("127.0.0.1", vp.PORT), timeout=1).close()
            time.sleep(0.5)  # 試しの接続が切れたのを見届けてもらう
            return p
        except OSError:
            time.sleep(0.3)
    raise SystemExit("vnc_probe が起動しない")


def ws_status(origin: str, host: str) -> int:
    """WebSocket への切り替えを頼み、返ってきた状態の番号 (101 = 受けた、403 = 断った)。"""
    with socket.create_connection(("127.0.0.1", vp.PORT), timeout=5) as s:
        s.sendall(f"GET /websockify HTTP/1.1\r\nHost: {host}\r\nOrigin: {origin}\r\nUpgrade: websocket\r\n"
                  "Connection: Upgrade\r\nSec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\nSec-WebSocket-Version: 13\r\n"
                  "Sec-WebSocket-Protocol: binary\r\n\r\n".encode())
        return int(s.recv(64).split(b" ")[1])


def start_app(tag: str, min_width: int | None = None) -> tuple[subprocess.Popen, int]:
    args = [sys.executable, str(HERE / "vnc_testapp.py"), tag] + ([str(min_width)] if min_width else [])
    app = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(60):
        # uv の .venv の python.exe は起動役で、窓を持つのは別のプロセスになる。プロセスの番号ではなく、
        # 窓の種類とタイトルの目印で探す。タイトルの頭だけで探したら、noVNC を開いたブラウザ (題がタイトルを写す) を
        # つかんで、ブラウザに文字を打ち込んだ
        hwnd = next((h for h in vp.top_windows() if vp.window_class(h) == "TkTopLevel"
                     and vp.window_text(h).startswith(f"vnc-testapp {tag} ")), 0)
        if hwnd:
            time.sleep(0.5)
            return app, hwnd
        time.sleep(0.2)
    raise SystemExit("試験用の窓が出ない")


def close_apps(apps) -> None:
    """試験用の窓を閉じ、プロセスが終わるのを待つ。終わらなければ起動役ごと止める
    (uv の .venv の python.exe は起動役で、窓を持つ本体は子プロセス。起動役だけ止めても本体が残る)。"""
    for _, h in apps:
        vp.user32.PostMessageW(h, vp.WM_CLOSE, 0, 0)
    for app, _ in apps:
        try:
            app.wait(10)
        except subprocess.TimeoutExpired:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(app.pid)], capture_output=True)


COVER = """
import ctypes, sys, tkinter as tk
ctypes.windll.shcore.SetProcessDpiAwareness(2)
r = tk.Tk(); r.title('vnc-cover'); r.geometry(sys.argv[1]); r.attributes('-topmost', True); r.configure(bg='gray')
r.mainloop()
"""


def start_cover(hwnd: int) -> subprocess.Popen:
    """試験用の窓の上に、最前面の窓をかぶせる (人がほかのアプリを開いている状態の代わり)。"""
    r = wintypes.RECT()
    ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(r))
    p = subprocess.Popen([sys.executable, "-c", COVER, f"{r.right - r.left + 40}x{r.bottom - r.top + 40}+{r.left - 20}+{r.top - 20}"])
    time.sleep(1.5)
    return p


def start_other() -> subprocess.Popen:
    """試験用の窓と重ならない所に、別の窓を開いて前面にする。"""
    sw = ctypes.windll.user32.GetSystemMetrics(0)
    p = subprocess.Popen([sys.executable, "-c", COVER.replace(", True", ", False"), f"300x200+{sw - 320}+40"])
    time.sleep(1.5)
    return p


def stop_probe(p: subprocess.Popen) -> None:
    p.send_signal(subprocess.signal.CTRL_BREAK_EVENT)
    try:
        p.wait(10)
    except subprocess.TimeoutExpired:
        p.kill()


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    OUT.mkdir(exist_ok=True)
    try:
        socket.create_connection(("127.0.0.1", vp.PORT), timeout=1).close()
        raise SystemExit(f"ポート {vp.PORT} が使われている (vnc_probe.py を止めてから流す)")
    except OSError:
        pass
    tag = f"t{os.getpid()}"
    app, hwnd = start_app(tag)
    original = vp.client_size(hwnd)
    probe = None
    try:
        probe = start_probe(hwnd)
        here = f"127.0.0.1:{vp.PORT}"
        check("WebSocket: よそのサイト (Origin) からは断る", ws_status("http://evil.example", here) == 403)
        check("WebSocket: 別の名前 (Host、DNS の書き換え) で来たら断る",
              ws_status(f"http://evil.example:{vp.PORT}", f"evil.example:{vp.PORT}") == 403)
        check("WebSocket: 自分が配ったページからは受ける", ws_status(f"http://{here}", here) == 101)
        c = Client(vp.PORT)
        img = c.full_frame()
        img.save(OUT / "1-initial.png")
        check("つないだときの画面の大きさ = 窓の内側", img.size == original, f"{img.size} / 窓 {original}")
        check("撮った画像に四隅が写る", not corners_ok(img), corners_ok(img))

        reply = c.set_size(412, 780)
        img = c.full_frame()
        img.save(OUT / "2-phone.png")
        check("412x780 の要求を受け付けた返事", reply == (1, 0, 412, 780), str(reply))
        check("窓の内側が 412x780 になった", vp.client_size(hwnd) == (412, 780), str(vp.client_size(hwnd)))
        check("412x780 の画像に四隅が写る", img.size == (412, 780) and not corners_ok(img), f"{img.size} {corners_ok(img)}")

        sh = ctypes.windll.user32.GetSystemMetrics(1)
        reply = c.set_size(500, sh + 600)
        img = c.full_frame()
        img.save(OUT / "3-taller-than-screen.png")
        # 枠のある窓は、Windows が画面の高さに収める (WM_GETMINMAXINFO)。VNC の画面は要求どおりにし、窓は上に寄せて写す
        cw, ch = vp.client_size(hwnd)
        check(f"画面 (高さ {sh}) より高い 500x{sh + 600} の要求: 返事と画像は要求どおり、窓は画面に収まる高さ",
              reply == (1, 0, 500, sh + 600) and img.size == (500, sh + 600) and cw == 500 and ch < sh,
              f"窓 {(cw, ch)} 返事 {reply} 画像 {img.size}")
        check("画面より高い要求: 窓の四隅が画像の上に寄せて写る", not corners_ok(img.crop((0, 0, cw, ch))),
              corners_ok(img.crop((0, 0, cw, ch))))
        c.set_size(412, 780)

        r = wintypes.RECT()
        ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(r))
        vp.user32.SetWindowPos(hwnd, None, -3000, 100, 0, 0, 0x0001 | vp.SWP_NOZORDER | vp.SWP_NOACTIVATE)
        time.sleep(0.3)
        img = c.full_frame()
        img.save(OUT / "4-offscreen.png")
        check("画面の外に出した窓も撮れる", not corners_ok(img), corners_ok(img))
        ctypes.windll.user32.ShowWindow(hwnd, 0)
        time.sleep(0.3)
        img = c.full_frame()
        img.save(OUT / "5-hidden.png")
        check("隠した窓 (ドックの選んでいないタブ) も撮れる", not corners_ok(img), corners_ok(img))
        ctypes.windll.user32.ShowWindow(hwnd, 8)  # SW_SHOWNA
        vp.user32.SetWindowPos(hwnd, None, r.left, r.top, 0, 0, 0x0001 | vp.SWP_NOZORDER | vp.SWP_NOACTIVATE)
        time.sleep(0.5)  # 出し直した直後は子窓がまだ隠れていて、押した先が外れる (ドックでは表に出ているタブにだけ送る)

        cur0 = wintypes.POINT()
        ctypes.windll.user32.GetCursorPos(ctypes.byref(cur0))
        fg0 = ctypes.windll.user32.GetForegroundWindow()
        for mask in (0, 1, 0):
            c.pointer(mask, 140, 110)
        title = wait_title(hwnd, "count=1")
        check("ボタンを押せる (窓へのメッセージ)", "count=1" in title, title)
        cur1 = wintypes.POINT()
        ctypes.windll.user32.GetCursorPos(ctypes.byref(cur1))
        check("手元のマウスカーソルは動かない", (cur0.x, cur0.y) == (cur1.x, cur1.y))
        check("手元の前面の窓は変わらない", ctypes.windll.user32.GetForegroundWindow() == fg0)
        for hold in (0.15, 0.6):  # スマホのタップ・長押し。Tk が本物のカーソルを見に行って「出た」ことにする間を挟む
            c.pointer(0, 140, 110)
            time.sleep(0.05)
            c.pointer(1, 140, 110)
            time.sleep(hold)
            c.pointer(0, 140, 110)
        title = wait_title(hwnd, "count=3")
        check("押したまま 0.15 秒・0.6 秒待ってから離しても押せる", "count=3" in title, title)

        other = start_other()  # 人が別のアプリを使っている状態。Tk は前面でないと文字を捨てる
        check("文字を打つ前に、試験用の窓は前面でない", ctypes.windll.user32.GetForegroundWindow() != hwnd)
        for mask in (0, 1, 0):
            c.pointer(mask, 210, 190)
        time.sleep(0.3)
        for ks in (ord("a"), ord("b"), ord("c"), 0x01003042, 0x01003044, 0xFF08):  # abc あい ← (BackSpace)
            c.key(ks)
        title = wait_title(hwnd, "text=abcあ ")
        check("文字・日本語 (Unicode の keysym)・BackSpace を打てる", "text=abcあ " in title, title)
        other.kill()
        c.close()
        time.sleep(0.5)
        stop_probe(probe)
        probe = None
        time.sleep(0.5)
        check("vnc_probe を止めると窓の大きさが元に戻る", vp.client_size(hwnd) == original,
              f"{vp.client_size(hwnd)} / 元 {original}")

        probe = start_probe(hwnd, "--zoom", "2")
        c = Client(vp.PORT)
        reply = c.set_size(824, 1560)
        img = c.full_frame()
        img.save(OUT / "6-zoom2.png")
        check("--zoom 2: 824x1560 の要求で、窓は 412x780", vp.client_size(hwnd) == (412, 780) and img.size == (824, 1560),
              f"窓 {vp.client_size(hwnd)} 画像 {img.size} 返事 {reply}")
        for mask in (0, 1, 0):
            c.pointer(mask, 280, 220)  # 窓の (140, 110) = ボタン
        title = wait_title(hwnd, "count=4")
        check("--zoom 2: 座標を半分にしてボタンを押せる", "count=4" in title, title)

        cover = start_cover(hwnd)
        for mask in (0, 1, 0):
            c.pointer(mask, 280, 220)
        title = wait_title(hwnd, "count=5", 1.5)
        check("(既知の制約) 最前面の窓が上に重なっていると、Tk のボタンは押せない", "count=4" in title, title)
        c.close()
        stop_probe(probe)
        probe = start_probe(hwnd, "--offscreen")
        c = Client(vp.PORT)
        c.full_frame()
        r = wintypes.RECT()
        ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(r))
        check("--offscreen: 窓が画面の外に出る", r.left >= ctypes.windll.user32.GetSystemMetrics(78), f"left={r.left}")
        for mask in (0, 1, 0):
            c.pointer(mask, 140, 110)
        title = wait_title(hwnd, "count=5")
        check("--offscreen: 元の場所に別の窓が重なっていても押せる", "count=5" in title, title)
        img = c.full_frame()
        img.save(OUT / "7-offscreen-mode.png")
        check("--offscreen: 撮った画像に四隅が写る", not corners_ok(img), corners_ok(img))
        c.close()
        stop_probe(probe)
        probe = None
        cover.kill()
        ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(r))
        check("--offscreen: 止めると元の場所に戻る", r.left < ctypes.windll.user32.GetSystemMetrics(78), f"left={r.left}")
        vp.user32.PostMessageW(hwnd, vp.WM_CLOSE, 0, 0)

        # 最小の幅が 600 の窓 (PortWho は 680、LPortMan は 760 より狭くならなかった)
        app2, hwnd2 = start_app(f"{tag}m", 600)
        probe = start_probe(hwnd2, "--offscreen")
        c = Client(vp.PORT)
        reply = c.set_size(412, 780)
        img = c.full_frame()
        img.save(OUT / "8-minwidth.png")
        cw, ch = vp.client_size(hwnd2)
        s = 412 / cw
        check("最小の幅 600 の窓に 412x780 を要求: 返事と画像は要求どおり、窓は幅 600 で縦に伸びる",
              reply == (1, 0, 412, 780) and img.size == (412, 780) and cw == 600 and ch > 780,
              f"窓 {(cw, ch)} 返事 {reply} 画像 {img.size}")
        fit = img.crop((0, 0, round(cw * s), round(ch * s)))
        check("最小の幅の窓: 縮めて四隅が収まる", not corners_ok(fit), f"{fit.size} {corners_ok(fit)}")
        for mask in (0, 1, 0):
            c.pointer(mask, round(140 * s), round(110 * s))
        title = wait_title(hwnd2, "count=1")
        check("最小の幅の窓: 縮めた画面の上でボタンを押せる", "count=1" in title, title)
        c.close()
        stop_probe(probe)
        probe = None
        vp.user32.PostMessageW(hwnd2, vp.WM_CLOSE, 0, 0)
        app2.wait(10)
    finally:
        if probe:
            stop_probe(probe)
        if hwnd:
            vp.user32.PostMessageW(hwnd, vp.WM_CLOSE, 0, 0)
        app.terminate()
    ng = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(ng)}/{len(results)} OK")
    sys.exit(1 if ng else 0)


if __name__ == "__main__":
    main()
