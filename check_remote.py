# /// script
# requires-python = ">=3.11"
# dependencies = ["pillow", "numpy", "websocket-client", "zxing-cpp"]
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
import ctypes.wintypes
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


def http_body(path: str) -> str:
    with socket.create_connection(("127.0.0.1", PORT), timeout=5) as s:
        s.sendall(f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{PORT}\r\n\r\n".encode())
        data = b""
        while chunk := s.recv(65536):
            data += chunk
    return data.split(b"\r\n\r\n", 1)[1].decode("utf-8", "replace")


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
        encs = [remote.ENC_ZLIB, remote.ENC_RAW, remote.PSEUDO_EXT_DESKTOP_SIZE, remote.PSEUDO_DESKTOP_SIZE,
                remote.PSEUDO_EXT_CLIPBOARD]
        self.send(struct.pack(f">BxH{len(encs)}i", 2, len(encs), *encs))
        self.size_msgs: list[tuple[int, int, int, int]] = []
        self.clips: list[str] = []  # PC から渡されたクリップボードの文字
        self.clip_flags: list[int] = []  # 届いた拡張クリップボードの印

    def send(self, data: bytes) -> None:
        self.ws.send_binary(data)

    def recv(self, n: int) -> bytes:
        while len(self.buf) < n:
            self.buf += self.ws.recv()
        out = bytes(self.buf[:n])
        del self.buf[:n]
        return out

    def read_update(self) -> bool:
        while (t := self.recv(1)[0]) == 3:  # ServerCutText (PC のクリップボード)
            self._cut_text()
        assert t == 0, t
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

    def _cut_text(self) -> None:
        n = struct.unpack(">3xi", self.recv(7))[0]
        body = self.recv(abs(n))
        if n >= 0:
            self.clips.append(body.decode("latin-1"))
            return
        flags = struct.unpack(">I", body[:4])[0]
        self.clip_flags.append(flags)
        if flags & remote.CLIP_CAPS:
            return  # できることの知らせ (渡す・頼むの印も立っている)
        if flags & remote.CLIP_PROVIDE:
            data = zlib.decompress(body[4:])
            size = struct.unpack(">I", data[:4])[0]
            self.clips.append(data[4:4 + size].rstrip(b"\0").decode("utf-8").replace("\r\n", "\n"))
        elif flags & remote.CLIP_REQUEST and self.paste_text is not None:
            # noVNC と同じく、頼まれてから渡す (変わったことを知らせ、PC が頼んでくる)。noVNC の Deflator は Z_FULL_FLUSH で
            # 終わりの印を付けない
            raw = self.paste_text.replace("\n", "\r\n").encode("utf-8") + b"\0"
            z = zlib.compressobj()
            data = z.compress(struct.pack(">I", len(raw)) + raw) + z.flush(zlib.Z_FULL_FLUSH)
            body = struct.pack(">I", remote.CLIP_PROVIDE | remote.CLIP_TEXT) + data
            self.send(struct.pack(">B3xi", 6, -len(body)) + body)
            self.paste_text = None

    paste_text: str | None = None

    def paste(self, text: str) -> None:
        """スマホの noVNC のクリップボードの欄に文字を入れたのと同じ。"""
        self.paste_text = text
        body = struct.pack(">I", remote.CLIP_NOTIFY | remote.CLIP_TEXT)
        self.send(struct.pack(">B3xi", 6, -len(body)) + body)

    def pump(self, seconds: float) -> None:
        """画面を頼んで読み続け、その間に届くクリップボードを受ける。"""
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            self.send(struct.pack(">BBHHHH", 3, 0, 0, 0, *self.fb.size))
            self.read_update()
            time.sleep(0.1)

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

    def key(self, keysym: int, down: int | None = None) -> None:
        """押して離す。down を渡すと、押すか離すかの片方だけ (Ctrl を押したままにする)"""
        for d in (1, 0) if down is None else (down,):
            self.send(struct.pack(">BB2xI", 4, d, keysym))

    def close(self) -> None:
        self.ws.close()


def steps(dock: td.Dock) -> None:
    app1, h1 = ck.start_app(f"r{os.getpid()}a")
    app2, h2 = ck.start_app(f"r{os.getpid()}b", 600)
    edit_app = start_edit_app(f"r{os.getpid()}e")
    apps: list = []  # 途中で開く試験用の窓 (最後に閉じる)
    c = None
    try:
        dock.call_in_tk(lambda: dock.adopt("app1", h1))
        dock.call_in_tk(lambda: dock.adopt("app2", h2))
        dock.call_in_tk(lambda: dock.select(dock.tabs[0]))
        original = tk_get(dock, dock.root.geometry)
        # Tk のスレッドに頼んだ処理が失敗しても、次の頼み事は届く (失敗で受け付けが止まり、試験ごと止まったことがある)
        dock.call_in_tk(lambda: 1 / 0)
        check("頼んだ処理が失敗しても、次の頼み事は届く", tk_get(dock, lambda: "届いた") == "届いた")
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
        kb = http_body("/core/input/keyboard.js")
        check("noVNC に手直しを当てて配る (iPad の日本語の入力中のキーを送らない)",
              "WinTabDock: keys typed while an IME composes" in kb, f"{len(kb)} 文字")
        check("noVNC の「追加のキー」(Ctrl・Alt など) が幅 0 に潰れないよう手直しして配る",
              "#noVNC_modifiers .noVNC_button {\n    max-width: none;" in http_body("/app/styles/base.css"))
        check("キーの記録 (URL に keylog) のスクリプトを読み込ませて配る",
              remote.KEYLOG_PATH in http_body("/vnc.html") and http_status("/" + remote.KEYLOG_PATH) == 200)
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
        time.sleep(1.2)  # 知らせの窓は 1 秒ごとに相手を出し直す
        notice = notice_state(dock)
        check("PC の画面には「スマホで使用中」の知らせが出る (画面の中・相手のアドレス・取り戻すボタン)",
              notice and notice["on_screen"] and any("127.0.0.1" in t for t in notice["texts"])
              and any("PC で使う" in t for t in notice["texts"]), str(notice))
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
        # 絵文字 (U+1F366)。noVNC は代理対の片方ずつ 2 回に分けて送ってくることがあり、片方だけを文字にしようとして接続ごと切れた
        keys = record_keys(dock)
        for ks in (0x0100D83C, 0x0100DF66, 0x0101F366):  # 片方ずつ 2 回 + 1 つの keysym
            c.key(ks)
        title = ck.wait_title(h1, "text=okあ🍦🍦 ")
        alive = bool(dock.remote_server and dock.remote_server.peers())
        check("絵文字を打てる (片方ずつ届いても 1 文字になり、接続が切れない)", "text=okあ🍦🍦 " in title and alive,
              f"{title} 接続 {alive} / 届いたキー {' '.join(keys)} / 渡せなかった {getattr(dock.remote_server.view.input, 'send_failures', '-')}")
        # スマホの日本語の入力は、変換中の「に ほ ん」を送ってから、確定で BackSpace 3 回と「日 本」を送ってくる。
        # BackSpace の後の文字が先に処理されると「にほん日本」から 3 文字消えて「にほ」が残った (Mery で)
        for ks in (0x0100306B, 0x0100307B, 0x01003093, 0xFF08, 0xFF08, 0xFF08, 0x010065E5, 0x0100672C):
            c.key(ks)
        title = ck.wait_title(h1, "text=okあ🍦🍦日本 ")
        check("日本語の入力の変換 (変換中の文字を BackSpace で消してから確定の文字) が、その順に届く",
              "text=okあ🍦🍦日本 " in title, title)
        # カタカナと「ー」「。」は、noVNC の表で X の古いカナの keysym (ニ = 0x04c6) になって届く。
        # Unicode の keysym しか文字に戻していなかったので捨てられ、カタカナに変換すると消えた
        for ks in (0x0100306B, 0x0100307B, 0x01003093, 0xFF08, 0xFF08, 0xFF08, 0x04C6, 0x04CE, 0x04DD, 0x04B0, 0x04A1):
            c.key(ks)
        title = ck.wait_title(h1, "text=okあ🍦🍦日本ニホンー。 ")
        check("カタカナに変換した字と「ー」「。」(noVNC は X のカナの keysym で送る) も届く",
              "text=okあ🍦🍦日本ニホンー。 " in title, title)
        # 修飾キー: Ctrl+S (入力欄に割り当て) で "[S]" が付き、Shift+Home で全体を選んで z で置き換える
        c.key(0xFFE3, 1)
        c.key(ord("s"))
        c.key(0xFFE3, 0)
        title = ck.wait_title(h1, "[S] ")
        check("Tk のツールでも Ctrl+S (ツールの割り当て) が効く", "text=okあ🍦🍦日本ニホンー。[S] " in title, title)
        c.key(0xFFE1, 1)
        c.key(0xFF50)
        c.key(0xFFE1, 0)
        c.key(ord("z"))
        title = ck.wait_title(h1, "text=z ")
        check("Tk のツールでも Shift+Home で選べ、Shift を離すと普通に打てる", "text=z " in title, title)

        mapped = tk_get(dock, lambda: (dock.menu_button.winfo_ismapped(), dock.settings_button.winfo_ismapped(),
                                       dock.open_button.winfo_ismapped(), dock.zoom_out_button.winfo_ismapped(),
                                       dock.zoom_in_button.winfo_ismapped()))
        check("つないでいる間は、帯の「+ 起動」「設定」の代わりに「+ 開く」と表示倍率のボタンが出る",
              mapped == (0, 0, 1, 1, 1), str(mapped))
        # 押せない所 (倍率の表示) は、押しても何も起きず、ドックも止まらない
        label_x, label_y = button_pos(dock, tk_get(dock, dock.zoom_label.winfo_id))
        menus_before = len(our_menus())
        c.tap(label_x, label_y)
        time.sleep(0.8)
        t0 = time.monotonic()
        dock.call_in_tk(lambda: None)
        stuck = time.monotonic() - t0
        menus = len(our_menus())
        check("帯の押せない所は、遠隔から押しても何も起きない (メニューが出ず、ドックが止まらない)",
              menus == menus_before and stuck < 1 and dock.remote_zoom == 1.0,
              f"メニュー {menus_before} → {menus} 個、応答 {stuck:.2f} 秒、倍率 {dock.remote_zoom}")

        for _ in range(3):  # 100 → 90 → 75 → 67%
            c.tap(*button_pos(dock, tk_get(dock, dock.zoom_out_button.winfo_id)))
            time.sleep(1.0)
        size = remote.client_size(dock.dock_hwnd)
        view = dock.remote_server.view
        # 1152 は画面の高さ (1080) より高い。Tk の maxsize を広げないと、Windows が画面の高さで止める
        check("「− 縮小」3 回で 67% になり、ドックは 411x772 の 1/0.67 の大きさになる (画面より高くても)",
              dock.remote_zoom == 0.67 and size == (613, 1152) and abs(view.scale - 0.67) < 0.01,
              f"倍率 {dock.remote_zoom} ドック {size} scale {view.scale:.3f}")
        # 画面の外で広げた所は、描かれるまで 0.5〜1 秒ほど黒い。帯も、前の倍率のボタンの絵がしばらく残る (実験で確かめた)。
        # 4 秒以内に正しく写れば良い
        deadline = time.monotonic() + 4
        while True:
            img = c.full_frame()
            _, tx, ty, tw, th = RemoteSource_layers(dock)[1]
            s = view.scale
            fit = img.crop((round(tx * s), round(ty * s), round((tx + tw) * s), round((ty + th) * s)))
            stale = old_zoom_buttons(img)
            if (not ck.corners_ok(fit) and is_light(fit) and not stale) or time.monotonic() > deadline:
                break
            time.sleep(0.2)
        img.save(OUT / "remote-1b-zoom67.png")
        check("縮小したあと、帯に前の倍率のボタンの絵が残らない", not stale, f"明るい画素 {stale}")
        check("縮小した画面にツールが収まる (四隅)", img.size == (411, 772) and not ck.corners_ok(fit),
              f"画像 {img.size} ツール {tw}x{th} {ck.corners_ok(fit)}")
        check("縮小した画面で、ツールの地が描かれている (中ほどが白い。画面の外で広げた所は描かれるのが遅れる)",
              is_light(fit), str(fit.getpixel((fit.width // 2, fit.height // 2))))
        c.tap(round((tx + 140) * s), round((ty + 110) * s))
        title = ck.wait_title(h1, "count=3")
        check("縮小した画面の上でも押せる", "count=3" in title, title)
        for _ in range(3):
            c.tap(*button_pos(dock, tk_get(dock, dock.zoom_in_button.winfo_id)))
            time.sleep(1.0)
        size = remote.client_size(dock.dock_hwnd)
        check("「+ 拡大」で 100% に戻り、ドックも 411x772 に戻る", dock.remote_zoom == 1.0 and size == (411, 772),
              f"倍率 {dock.remote_zoom} ドック {size}")

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
            if (not ck.corners_ok(fit) and is_light(fit)) or time.monotonic() > deadline:
                break
            time.sleep(0.2)
        img.save(OUT / "remote-2-minwidth.png")
        # 切り替えた直後 (0.2 秒以内) のタップは、30 回に 1 回ほど抜けた (窓を出して大きさを変えた直後)。少し待ってから押す
        time.sleep(0.8)
        # Tk の最小の幅は枠を含めた窓の大きさに効くので、枠を外したツールの内側は 600 より少し広い (616 だった)
        check("最小の幅 600 のツールは、縮めて収まる (四隅)", tw >= 600 and img.size == (411, 772) and not ck.corners_ok(fit),
              f"ツール {tw}x{th} 画像 {img.size} {ck.corners_ok(fit)}")
        check("切り替えて広げたツールの地が描かれている (中ほどが白い。3 秒待つ前は真っ黒のことがあった)", is_light(fit),
              str(fit.getpixel((fit.width // 2, fit.height // 2))))
        c.tap(round((tx + 140) * s), round((ty + 110) * s))
        title = ck.wait_title(h2, "count=1")
        check("縮めた画面の上でも押せる", "count=1" in title, title)
        edit_steps(dock, c, edit_app)
        clipboard_steps(dock, c, edit_app)
        menu_steps(dock, c, edit_app)
        find_dlg = find_steps(dock, c, edit_app)
        # つないでいる間に開いた外のアプリの窓 (RepoTether が開く VS Code など) は、PC の画面に出てスマホから見えなかった。
        # 登録していない別のプロセスの窓を開くと、タブになって選ばれる
        later = ck.start_app(f"r{os.getpid()}c")
        apps.append(later)
        for _ in range(50):
            if dock.active and dock.active.hwnd == later[1]:
                break
            time.sleep(0.1)
        layer = RemoteSource_layers(dock)[1] if dock.active else None
        check("つないでいる間に開いた別のアプリの窓は、タブになって選ばれ、遠隔の画面に出る",
              bool(dock.active) and dock.active.hwnd == later[1] and layer[0] == later[1] and layer[1] >= 0,
              f"選んでいるタブ {dock.active and dock.active.name} {layer}")
        launch_steps(dock, c, apps)
        maximize_steps(dock, apps)
        second_window_steps(dock, apps)

        c.close()
        c = None
        time.sleep(1.5)
        now = tk_get(dock, dock.root.geometry)
        check("切ると、ドックが元の大きさ・位置に戻る", now == original, f"{now} / 元 {original}")
        if find_dlg:
            fx = td.rect_of(find_dlg)[0]
            vl = td.user32.GetSystemMetrics(76)
            check("切ると、遠隔の画面へ移した小窓は PC の画面の中に戻る",
                  vl <= fx < vl + td.user32.GetSystemMetrics(78), f"小窓 {td.rect_of(find_dlg)}")
        check("つないでいる間にタブにした窓は、切ってもタブのまま残る", any(t.hwnd == later[1] for t in dock.tabs),
              str([t.name for t in dock.tabs]))
        check("つないでいない間に開いた窓は、タブにしない (自動取り込みが切ってある試験のドック)",
              not_adopted_when_disconnected(dock, apps), "")
        dock.call_in_tk(dock.save_session)
        saved = json.loads((HOME / "session.json").read_text(encoding="utf-8"))["geometry"]
        check("前回の位置には、元の位置を覚える", saved == original, saved)
        check("切ると、知らせの窓も消える", notice_state(dock) is None)
        panel_steps(dock, token)

        # 知らせの窓の「PC で使う」で取り戻す。待ち受けごと止める (つなぎ直してこない)
        c = Client(dock.prefs["remote_token"])
        c.full_frame()
        time.sleep(1.0)
        tk_get(dock, lambda: next(w for w in all_widgets(dock._remote_notice)
                                  if isinstance(w, td.tk.Button) and "PC で使う" in w.cget("text")).invoke())
        time.sleep(1.0)
        now = tk_get(dock, dock.root.geometry)
        check("「PC で使う」で、待ち受けが止まり、ドックが元の場所に戻り、知らせが消える",
              dock.remote_server is None and not dock.prefs.get("remote") and now == original and notice_state(dock) is None,
              f"待ち受け {dock.remote_server} ドック {now} 知らせ {notice_state(dock)}")
        c.close()
        c = None
    except Exception:
        traceback.print_exc()
        check("途中で止まらない", False)
    finally:
        if c:
            c.close()
        # 試験用の窓を先に閉じてから、ドックを閉じる。ドックを閉じるとこのプロセスが終わり、このスレッドも止まる
        # (先にドックを閉じていたら、窓を閉じる前に止まり、試験用の窓が 90 個残った)
        ck.close_apps([(app1, h1), (app2, h2), edit_app] + apps)
        dock.call_in_tk(dock.stop_remote)
        dock.call_in_tk(dock.quit, wait=False)


def launch_steps(dock: td.Dock, c: Client, apps: list) -> None:
    """スマホの「+ 開く」: ドックの中にツールの一覧を出し、起動・戻る・選んでいるツールを閉じる。"""
    launched: list[str] = []
    dock.launch = lambda tool, new=False: launched.append(tool["name"])  # 試験では起動しない (押されたかだけ見る)
    dock.tools = [{"name": "fake", "exe": r"C:\nonexistent\fake.exe"}]

    def panel_buttons() -> dict[str, int]:
        return tk_get(dock, lambda: {w.cget("text"): w.winfo_id() for w in all_widgets(dock.launch_panel)
                                     if isinstance(w, td.tk.Button)} if dock.launch_panel else {})

    def open_panel() -> dict[str, int]:
        c.tap(*button_pos(dock, tk_get(dock, dock.open_button.winfo_id)))
        time.sleep(1.0)
        return panel_buttons()

    def shown() -> int:  # 遠隔の画面に写しているツール (無ければ 0)
        layers = RemoteSource_layers(dock)
        return layers[1][0] if len(layers) > 1 else 0

    try:
        active = dock.active
        buttons = open_panel()
        img = c.full_frame()
        img.save(OUT / "remote-3-launch.png")
        names = [t.name + (" (表示中)" if t is active else "") for t in dock.tabs]
        check("「+ 開く」で、ドックの中にツールの一覧 (開いているツール・起動する・戻る・閉じる) が出て、選んでいたツールは隠れる",
              set(buttons) == {*names, "起動: fake", "戻る", f"「{active.name}」を閉じる"} and shown() == 0,
              f"{list(buttons)} 写しているツール {shown()}")
        # 帯の右端のボタンは、タブが多くても切れない (タブが 4 つで「+ 拡大」が切れていた)
        right = td.rect_of(tk_get(dock, dock.zoom_in_button.winfo_id))
        ox = remote.client_origin(dock.dock_hwnd)[0]
        check("タブが多くても、帯の「+ 拡大」はドックの中に収まる",
              right[0] + right[2] <= ox + remote.client_size(dock.dock_hwnd)[0], f"{right} ドックの左 {ox}")
        c.tap(*button_pos(dock, buttons["起動: fake"]))
        time.sleep(1.0)
        check("一覧の「起動: …」を押すとそのツールを開き、一覧が消えて元のツールが写る",
              launched == ["fake"] and dock.launch_panel is None and shown() == active.hwnd,
              f"開いた {launched} 一覧 {dock.launch_panel} 写しているツール {shown()}")
        buttons = open_panel()
        c.tap(*button_pos(dock, buttons["戻る"]))
        time.sleep(1.0)
        check("「戻る」で一覧が消え、元のツールが写る", dock.launch_panel is None and shown() == active.hwnd,
              f"一覧 {dock.launch_panel} 写しているツール {shown()}")
        buttons = open_panel()
        c.tap(*button_pos(dock, buttons[dock.tabs[1].name]))
        time.sleep(1.0)
        check("一覧の「開いているツール」を押すと、そのツールに切り替わる",
              dock.launch_panel is None and dock.active is dock.tabs[1] and shown() == dock.tabs[1].hwnd,
              f"一覧 {dock.launch_panel} 写しているツール {shown()}")
        open_panel()
        c.tap(*button_pos(dock, dock.tabs[0].button_hwnd))
        time.sleep(1.0)
        check("一覧を開いたまま帯のタブを押すと、一覧が消えてそのタブのツールが写る",
              dock.launch_panel is None and dock.active is dock.tabs[0] and shown() == dock.tabs[0].hwnd,
              f"一覧 {dock.launch_panel} 写しているツール {shown()}")
        # 選んでいるツールを閉じる (つないでいる間に開いた窓はタブになって選ばれる)
        doomed = ck.start_app(f"r{os.getpid()}f")
        apps.append(doomed)
        for _ in range(50):
            if dock.active and dock.active.hwnd == doomed[1]:
                break
            time.sleep(0.1)
        buttons = open_panel()
        name = dock.active.name if dock.active else "-"
        c.tap(*button_pos(dock, buttons.get(f"「{name}」を閉じる", 0)))
        for _ in range(50):
            if not td.user32.IsWindow(doomed[1]) and all(t.hwnd != doomed[1] for t in dock.tabs):
                break
            time.sleep(0.1)
        check("一覧の「「…」を閉じる」で、選んでいるツールが閉じてタブも消え、ほかのタブのツールが写る",
              not td.user32.IsWindow(doomed[1]) and all(t.hwnd != doomed[1] for t in dock.tabs)
              and dock.launch_panel is None and shown() not in (0, doomed[1]),
              f"窓 {td.user32.IsWindow(doomed[1])} タブ {[t.name for t in dock.tabs]} 写しているツール {shown()}")
    finally:
        del dock.launch
        dock.tools = []


def maximize_steps(dock: td.Dock, apps: list) -> None:
    """タブにしたあとでツールが自分で最大化しても、帯の下に収め直す。VS Code は開いた少しあとで前回の最大化に戻し、
    画面いっぱいのまま重なって、スマホでは帯ごと隠れた。外すときは最大化に戻す"""
    app = ck.start_app(f"r{os.getpid()}m")
    apps.append(app)
    hwnd = app[1]
    for _ in range(50):
        if dock.active and dock.active.hwnd == hwnd:
            break
        time.sleep(0.1)
    td.user32.ShowWindow(hwnd, 3)  # SW_MAXIMIZE (ツールが自分で最大化したのと同じ)
    for _ in range(30):
        time.sleep(0.1)
        if not td.user32.IsZoomed(hwnd):
            break
    time.sleep(0.5)
    layer = next((lay for lay in RemoteSource_layers(dock) if lay[0] == hwnd), None)
    body_y = tk_get(dock, dock.body_rect)[1] - remote.client_origin(dock.dock_hwnd)[1]
    check("タブにしたあとでツールが最大化しても、最大化を解いて帯の下に収め直す",
          not td.user32.IsZoomed(hwnd) and layer is not None and layer[1] == 0 and layer[2] == body_y,
          f"最大化 {td.user32.IsZoomed(hwnd)} 写す位置 {layer} 帯の下 {body_y}")
    tab = next(t for t in dock.tabs if t.hwnd == hwnd)
    dock.call_in_tk(lambda: dock.release(tab))
    time.sleep(0.5)
    check("最大化していたツールは、ドックから外すと最大化に戻る", bool(td.user32.IsZoomed(hwnd)),
          f"最大化 {td.user32.IsZoomed(hwnd)} {td.rect_of(hwnd)}")
    td.user32.ShowWindow(hwnd, 0)  # 人の画面に出したままにしない (最後に閉じる)
    # 最大化した窓をタブにしても、最大化を解いて収める
    td.user32.ShowWindow(hwnd, 3)
    time.sleep(0.5)
    dock.ignored.discard(hwnd)
    dock.call_in_tk(lambda: dock.adopt("max", hwnd))
    time.sleep(0.8)
    layer = next((lay for lay in RemoteSource_layers(dock) if lay[0] == hwnd), None)
    tab = next((t for t in dock.tabs if t.hwnd == hwnd), None)
    check("最大化した窓をタブにしても、最大化を解いて帯の下に収め、最大化していたことを覚える",
          not td.user32.IsZoomed(hwnd) and layer is not None and layer[2] == body_y and tab is not None and tab.maximized,
          f"最大化 {td.user32.IsZoomed(hwnd)} 写す位置 {layer} 覚えた {tab and tab.maximized}")


def second_window_steps(dock: td.Dock, apps: list) -> None:
    """同じプロセスのもう 1 つの普通の窓 (VS Code はどの窓も 1 つのプロセス)。小窓としてドックの上へ移して重ねると、
    スマホの画面で帯ごと覆った。つないでいる間に開いたら別のタブにし、タブにしていなければ重ねない"""
    tag = f"r{os.getpid()}s"
    os.environ["VNC_TESTAPP_SECOND"] = "1"
    try:
        app = ck.start_app(tag)
    finally:
        del os.environ["VNC_TESTAPP_SECOND"]
    apps.append(app)
    main = app[1]
    second = 0
    for _ in range(50):
        # タブにした窓は隠れるので、top_windows (見えている窓) ではなくタブから探す
        second = next((h for h in [t.hwnd for t in dock.tabs] + td.top_windows()
                       if td.window_title(h) == f"vnc-testapp {tag}-second"), 0)
        if second and {main, second} <= {t.hwnd for t in dock.tabs}:
            break
        time.sleep(0.1)
    check("つないでいる間に開いた、同じプロセスのもう 1 つの普通の窓は、別のタブになる",
          bool(second) and {main, second} <= {t.hwnd for t in dock.tabs}, str([t.name for t in dock.tabs]))
    tab2 = next((t for t in dock.tabs if t.hwnd == second), None)
    if tab2:
        dock.call_in_tk(lambda: dock.release(tab2))  # 人の画面にある、タブにしていない窓にする (報告の形)
    time.sleep(0.5)
    before = td.rect_of(second)
    dock.call_in_tk(lambda: dock.select(next(t for t in dock.tabs if t.hwnd == main)))
    time.sleep(1.0)
    layers = [lay[0] for lay in RemoteSource_layers(dock)]
    check("タブにしていない、同じプロセスの普通の窓は、ドックの上へ移さず、スマホの画面にも重ねない",
          second not in layers and td.rect_of(second) == before and main in layers,
          f"重ねる窓 {layers} 窓の位置 {before} → {td.rect_of(second)}")


def panel_steps(dock: td.Dock, token: str) -> None:
    """「スマホから使う」の画面: 状態・アドレス・URL を見せ、選んだネットワークで待ち受け直す。"""
    dock.call_in_tk(dock.show_remote_panel)
    time.sleep(1.2)
    texts = tk_get(dock, lambda: widget_texts(dock._remote_panel))
    radios = tk_get(dock, lambda: [(w.cget("value"), str(w.cget("state"))) for w in radio_buttons(dock._remote_panel)])
    url_shown = tk_get(dock, lambda: [w.get() for w in all_widgets(dock._remote_panel) if isinstance(w, td.tk.Entry)])
    shot(dock, "remote-3-panel.png")
    check("画面に待ち受けの状態とアドレスが出る", any("待ち受け中  127.0.0.1:15900" in t for t in texts)
          and any("wintabdock.toml" in t for t in texts), " / ".join(t for t in texts if "待ち受け" in t))
    check("toml で決めているときは、アドレスを選べない", radios and all(s == "disabled" for _, s in radios), str(radios))
    check("URL の合言葉は画面に出さない (伏せ字)", url_shown and token not in url_shown[0] and "********" in url_shown[0],
          url_shown[0] if url_shown else "-")
    check("この PC だけで待ち受けているときは、QR コードを出さない", not read_qr(OUT / "remote-3-panel.png"),
          str(read_qr(OUT / "remote-3-panel.png")))

    # Wi-Fi を選ぶ (ファイアウォールで許可した、家のネットワーク)。仮想のネットワーク (VirtualBox・WSL) で待ち受けると、
    # 許可の決まりが無いので Windows の確認が人の画面に出る
    nets = sorted(remote.local_addresses(), key=lambda a: a[0] != "Wi-Fi")[:1]
    if not nets:
        check("ほかのネットワークを選ぶ (この PC にネットワークが無いので飛ばす)", True)
        return
    try:  # 人が使っている WinTabDock が、そのアドレスで待ち受けていることがある (ぶつかって待ち受けられない)。邪魔しない
        socket.create_connection((nets[0][1], PORT), timeout=1).close()
        check(f"ほかのネットワークを選ぶ ({nets[0][1]}:{PORT} はほかで使われているので飛ばす)", True)
        return
    except OSError:
        pass
    dock.remote_settings.pop("host")  # toml に書いていない人として
    name, ip = nets[0]
    deadline = time.monotonic() + 3  # 画面は 1 秒ごとに並べ直す。決め打ちの 1.5 秒待ちでは 1 度間に合わなかった
    while True:
        radios = tk_get(dock, lambda: [(w.cget("value"), str(w.cget("state"))) for w in radio_buttons(dock._remote_panel)])
        if (radios and all(s == "normal" for _, s in radios)) or time.monotonic() > deadline:
            break
        time.sleep(0.2)
    check("toml の host を消すと、アドレスを選べるようになる", radios and all(s == "normal" for _, s in radios), str(radios))
    tk_get(dock, lambda: next(w for w in radio_buttons(dock._remote_panel) if w.cget("value") == name).invoke())
    host = wait_host(dock, ip)
    check(f"「{name}」を選ぶと、そのアドレスで待ち受け直す", host == ip and dock.prefs.get("remote_network") == name,
          f"{host} / {ip}")
    time.sleep(1.2)
    texts = tk_get(dock, lambda: widget_texts(dock._remote_panel))
    shot(dock, "remote-4-panel-lan.png")
    check("画面のアドレスも変わる", any(f"待ち受け中  {ip}:" in t for t in texts),
          " / ".join(t for t in texts if "待ち受け" in t))
    codes = read_qr(OUT / "remote-4-panel-lan.png")
    url = dock.remote_server.url
    check("QR コードを読むと、合言葉入りの URL になる", codes == [url],
          str([c.replace(token, "<合言葉>") for c in codes]))
    tk_get(dock, lambda: next(w for w in radio_buttons(dock._remote_panel) if w.cget("value") == "loopback").invoke())
    host = wait_host(dock, "127.0.0.1")
    check("「この PC だけ」に戻せる", host == "127.0.0.1", str(host))
    dock.call_in_tk(dock._remote_panel.destroy)


def notice_state(dock: td.Dock) -> dict | None:
    """つないでいる間の知らせの窓。無ければ None。"""
    def get():
        win = dock._remote_notice
        if not win or not win.winfo_exists():
            return None
        x, y, w, h = td.rect_of(int(win.wm_frame(), 16))
        vx, vy = td.user32.GetSystemMetrics(76), td.user32.GetSystemMetrics(77)
        vw, vh = td.user32.GetSystemMetrics(78), td.user32.GetSystemMetrics(79)
        return {"on_screen": vx <= x and x + w <= vx + vw and vy <= y and y + h <= vy + vh and win.winfo_viewable(),
                "rect": (x, y, w, h), "texts": widget_texts(win)}
    return tk_get(dock, get)


def wait_host(dock: td.Dock, want: str) -> str | None:
    for _ in range(50):
        if dock.remote_server and dock.remote_server.host == want:
            break
        time.sleep(0.1)
    return dock.remote_server.host if dock.remote_server else None


def all_widgets(w) -> list:
    out = [w]
    for c in w.winfo_children():
        out += all_widgets(c)
    return out


def radio_buttons(w) -> list:
    return [x for x in all_widgets(w) if isinstance(x, td.tk.Radiobutton)]


def widget_texts(w) -> list[str]:
    return [str(x.cget("text")) for x in all_widgets(w) if isinstance(x, (td.tk.Label, td.tk.Button, td.tk.Radiobutton))]


def start_edit_app(tag: str):
    """Tk でない試験用の窓 (experiments/edit_testapp.py。Windows の EDIT 部品だけ) を起動して (プロセス, 窓) を返す。
    起動役 (uv の python.exe) と窓を持つプロセスが別なので、タイトルで探す"""
    import subprocess
    title = f"edit-testapp {tag}"
    proc = subprocess.Popen([sys.executable, str(Path(__file__).resolve().parent / "experiments" / "edit_testapp.py"), tag])
    for _ in range(100):
        found = [h for h in all_windows() if window_class(h) == "EditTestApp" and td.window_title(h) == title]
        if found:
            return proc, found[0]
        time.sleep(0.1)
    raise RuntimeError(f"{title} の窓が出ない")


def edit_steps(dock: td.Dock, c, edit_app) -> None:
    """Tk でない窓 (EDIT。BackSpace などを WM_CHAR で処理する作り) に、スマホの日本語の入力と同じ並びで打つ。
    Mery で、BackSpace の後の文字が先に処理されて「にほ」が残った (受け取りの列に置いていたので、翻訳で後ろに作られる
    WM_CHAR(8) に追い越された)"""
    _, he = edit_app
    dock.call_in_tk(lambda: dock.adopt("edit", he))
    time.sleep(1.0)
    tab_x, tab_y = button_pos(dock, dock.tabs[-1].button_hwnd)
    c.tap(tab_x, tab_y)
    time.sleep(1.5)
    view = dock.remote_server.view
    _, tx, ty, tw, th = RemoteSource_layers(dock)[1]
    s = view.scale
    c.tap(round((tx + tw // 2) * s), round((ty + th // 2) * s))
    time.sleep(0.5)
    seq = [ord("a"), ord("b"), ord("c"), 0xFF0D,
           0x0100306B, 0x0100307B, 0x01003093, 0x01003054, 0xFF08, 0xFF08, 0xFF08, 0xFF08,  # に ほ ん ご → 消す
           0x010065E5, 0x0100672C, 0x01008A9E, 0x0100D83C, 0x0100DF66,  # 日 本 語 🍦 (代理対の片方ずつ)
           0x0100306B, 0x0100307B, 0xFF08, 0xFF08, 0x04C6, 0x04CE, 0x04B0,  # に ほ → カタカナの ニ ホ ー
           0xFF09, ord("x"), 0xFF08, 0xFF51, 0xFFFF]  # Tab x BackSpace ← Delete (Tab も消える)
    for ks in seq:
        c.key(ks)
    time.sleep(1.0)
    text = edit_text(he)
    want = "abc\r\n日本語🍦ニホー"
    check("Tk でない窓 (EDIT) にも、日本語の変換・カタカナ・絵文字・Enter・Tab・BackSpace・矢印・Delete が送った順に効く",
          text == want, f"{text!r} / 期待 {want!r}")
    # 修飾キー: Shift+Home で行を選び、Ctrl+X で切り取って Ctrl+V で 2 回貼る。Ctrl+S はショートカットの表で "[S]" を足す。
    # Ctrl を離した後の g は、ただの g になる
    for ks in (0xFF0D, ord("d"), ord("e"), ord("f")):
        c.key(ks)
    c.key(0xFFE1, 1)
    c.key(0xFF50)
    c.key(0xFFE1, 0)
    c.key(0xFFE3, 1)
    for ks in (ord("x"), ord("v"), ord("v"), ord("s")):
        c.key(ks)
    c.key(0xFFE3, 0)
    c.key(ord("g"))
    time.sleep(1.0)
    text = edit_text(he)
    check("Tk でない窓 (EDIT) で、Shift+Home・Ctrl+X・Ctrl+V と、ショートカットの表の Ctrl+S が効き、Ctrl を離すと元に戻る",
          text == want + "\r\ndefdef[S]g", f"{text!r}")


def clipboard_steps(dock: td.Dock, c, edit_app) -> None:
    """PC とスマホのクリップボード。edit_steps の Ctrl+X で、PC のクリップボードは "def" になっている"""
    _, he = edit_app
    c.pump(1.5)
    check("拡張クリップボード (UTF-8) を使えることを知らせる",
          any(f & remote.CLIP_CAPS and f & remote.CLIP_TEXT for f in c.clip_flags), str([hex(f) for f in c.clip_flags]))
    check("ツールで写した (Ctrl+X) PC のクリップボードが、スマホに渡る", c.clips[-1:] == ["def"], repr(c.clips[-3:]))
    phone = "スマホから🍦\n2 行目"
    before = len(c.clips)
    c.paste(phone)
    c.pump(1.5)
    pc = remote.clipboard_text()
    check("スマホのクリップボードの欄の文字 (日本語・絵文字・改行) が、PC のクリップボードに入る",
          pc == phone.replace("\n", "\r\n"), repr(pc))
    check("スマホから入れた文字は、スマホに送り返さない", len(c.clips) == before, repr(c.clips[before:]))
    c.key(0xFFE3, 1)
    c.key(ord("v"))
    c.key(0xFFE3, 0)
    time.sleep(1.0)
    text = edit_text(he)
    check("スマホから入れた文字を、Ctrl+V でツールに貼れる", text.endswith("g" + phone.replace("\n", "\r\n")), repr(text[-20:]))
    remote.set_clipboard_text("PC で写した")
    c.pump(1.5)
    check("PC で写した文字も、スマホに渡る", c.clips[-1:] == ["PC で写した"], repr(c.clips[-3:]))


def not_adopted_when_disconnected(dock: td.Dock, apps: list) -> bool:
    """つないでいないときに別のアプリの窓を開いても、タブにならない。"""
    other = ck.start_app(f"r{os.getpid()}d")
    apps.append(other)
    time.sleep(1.5)
    return not any(t.hwnd == other[1] for t in dock.tabs)


def find_steps(dock: td.Dock, c, edit_app) -> int:
    """ツールの小窓 (Mery の検索)。Mery は、持ち主の窓 (画面の外) の近くに出そうとして画面の中に収めるので、PC の画面の
    右端に出て、遠隔の画面の外になった。試験用の窓の「検索...」も同じく収めて出す。小窓を返す (切った後に見る)"""
    _, he = edit_app
    view = dock.remote_server.view
    s = view.scale
    _, tx, ty, tw, th = RemoteSource_layers(dock)[1]
    c.tap(round((tx + 20) * s), round((ty + 10) * s))
    menus = []
    for _ in range(60):
        menus = find_menus(he)
        if menus and remote.client_origin(menus[0])[0] >= td.user32.GetSystemMetrics(78):
            break
        time.sleep(0.05)
    dlg = 0
    if menus:
        x, y = menu_item_pos(dock, menus[0], 2)  # 「検索...」
        c.tap(round(x * s), round(y * s))
        for _ in range(60):
            dlg = next((h for h in all_windows() if td.window_title(h) == "edit-testapp-find"
                        and td.user32.IsWindowVisible(h)), 0)
            if dlg:
                break
            time.sleep(0.05)
    time.sleep(0.5)
    w, h = remote.client_size(dock.dock_hwnd)
    layer = next((lr for lr in RemoteSource_layers(dock) if dlg and lr[0] == dlg), None)
    check("ツールが PC の画面の中に収めて出した小窓 (Mery の検索など) も、遠隔の画面の中に入る",
          bool(layer) and 0 <= layer[1] and layer[1] + layer[3] <= w and 0 <= layer[2] and layer[2] + layer[4] <= h,
          f"小窓 {layer} ドック {w}x{h} 画面での位置 {dlg and td.rect_of(dlg)}")
    for ch in "find":
        c.key(ord(ch))
    time.sleep(0.5)
    box = ctypes.windll.user32.FindWindowExW(dlg, None, "EDIT", None) if dlg else 0
    buf = ctypes.create_unicode_buffer(32)
    if box:
        ctypes.windll.user32.SendMessageW(box, 0x000D, 32, buf)  # WM_GETTEXT
    check("小窓に打てる", buf.value == "find", repr(buf.value))
    return dlg


def edit_text(he: int) -> str:
    edit = ctypes.windll.user32.FindWindowExW(he, None, "EDIT", None)
    n = td.user32.SendMessageW(edit, 0x000E, 0, 0)  # WM_GETTEXTLENGTH
    buf = ctypes.create_unicode_buffer(n + 1)
    ctypes.windll.user32.SendMessageW(edit, 0x000D, n + 1, buf)  # WM_GETTEXT
    return buf.value


def find_menus(top: int) -> list[int]:
    """top と同じスレッドの、見えているメニューの窓 (#32768。一番上の段から)。"""
    tid = td.user32.GetWindowThreadProcessId(top, None)
    return [h for h in all_windows()[::-1] if window_class(h) == "#32768" and td.user32.IsWindowVisible(h)
            and td.user32.GetWindowThreadProcessId(h, None) == tid]


def menu_item_pos(dock: td.Dock, menu: int, i: int) -> tuple[int, int]:
    """メニューの i 番目の項目の真ん中 (ドックの内側の座標)。"""
    hmenu = ctypes.windll.user32.SendMessageW(menu, 0x01E1, 0, 0)  # MN_GETHMENU
    r = ctypes.wintypes.RECT()
    ctypes.windll.user32.GetMenuItemRect(None, ctypes.c_void_p(hmenu), i, ctypes.byref(r))
    ox, oy = remote.client_origin(dock.dock_hwnd)
    return (r.left + r.right) // 2 - ox, (r.top + r.bottom) // 2 - oy


def menu_steps(dock: td.Dock, c, edit_app) -> None:
    """Mery のメニューの帯と同じく、押した時にメニューを出す窓 (experiments/edit_testapp.py の上の帯)。メニューは別の窓
    (#32768) で、ドックが画面の外にあると Windows が PC の画面の中へ寄せて出し、遠隔からは見えなかった"""
    _, he = edit_app
    view = dock.remote_server.view
    s = view.scale
    _, tx, ty, tw, th = RemoteSource_layers(dock)[1]
    before = edit_text(he)

    # 直す前の作り (メニューを扱わない remote.py) でも、例外で止まらずに落ちるようにする
    view_layers = getattr(view, "layers", view.source.layers)

    def wait_menus(n: int) -> list[int]:
        for _ in range(60):
            menus = find_menus(he)
            placed = view.menus.placed if hasattr(view, "menus") else {}
            if len(menus) == n and all(h in placed for h in menus):
                return menus
            view_layers()
            time.sleep(0.05)
        return find_menus(he)

    def tap(x: float, y: float) -> None:
        c.tap(round(x * s), round(y * s))

    tap(tx + 20, ty + 10)  # 帯の「ファイル」
    menus = wait_menus(1)
    right = td.user32.GetSystemMetrics(76) + td.user32.GetSystemMetrics(78)
    rects = [td.rect_of(h) for h in menus]
    check("ツールのメニューが開き、PC の画面ではなく画面の外 (ドックの横) に出る",
          len(menus) == 1 and rects[0][0] >= right, f"{rects} / 画面の右端 {right}")
    layer = next((lr for lr in view_layers() if menus and lr[0] == menus[0]), None)
    img = c.full_frame()
    img.save(OUT / "remote-5-menu.png")
    shown = layer and img.crop((round(layer[1] * s), round(layer[2] * s), round((layer[1] + layer[3]) * s),
                                round((layer[2] + layer[4]) * s)))
    # 帯の高さは 28。メニューの内側は、窓の縁の分だけ下
    check("メニューが遠隔の画面に写り、出るはずの所 (帯の下) にある",
          bool(layer) and ty + 26 <= layer[2] <= ty + 40 and is_light(shown), f"メニュー {layer} ツール {(tx, ty)}")
    if len(menus) != 1:
        return

    t0 = time.monotonic()
    tap(*menu_item_pos(dock, menus[0], 1))  # 「その他」(下の段を開く)
    menus = wait_menus(2)
    opened = time.monotonic() - t0
    if len(menus) == 2:
        tap(*menu_item_pos(dock, menus[1], 0))  # 「SUB を挿入」
    time.sleep(0.5)
    text = edit_text(he)
    # 押した時にメニューを出すツールは、閉じるまで「押した」の処理から戻らない。待ち続けると受け取りが 5 秒止まった
    check("メニューの下の段を開いて、その項目を選べる (開くのに 2 秒かからない)",
          text == before + "SUB" and opened < 2 and not find_menus(he), f"{text!r} 開くまで {opened:.1f} 秒")

    tap(tx + 20, ty + 10)
    wait_menus(1)
    tap(tx + 3, ty + 36)  # メニューの外 (EDIT の 1 行目の頭。ツールに届けば、文字を足す所がそこへ動く)
    time.sleep(0.5)
    closed = not find_menus(he)
    tap(tx + 20, ty + 10)
    wait_menus(1)
    for ks in (0xFF54, 0xFF0D):  # ↓ Enter
        c.key(ks)
    time.sleep(0.5)
    text = edit_text(he)
    check("メニューの外を押すと閉じ、その押し込みはツールに届かない", closed and text.startswith("abc"),
          f"閉じた {closed} {text!r}")
    check("メニューを開いている間のキー (↓ Enter) はメニューに効く",
          text == before + "SUBXYZ" and not find_menus(he), repr(text))


def record_keys(dock: td.Dock) -> list[str]:
    """届いたキーと、渡した先の窓 (Tk か) を控える。落ちたときに出す。"""
    keys: list[str] = []
    inp = dock.remote_server.view.input
    orig = inp.key

    def key(down: bool, keysym: int) -> None:
        top = inp.view.source.key_window()
        dest = remote.focus_of(top) if top else 0
        if down:
            keys.append(f"{keysym:#x}→{dest}{'(Tk)' if dest and getattr(remote, 'is_tk', lambda h: False)(dest) else ''} 前半={getattr(inp, 'high_surrogate', 0):#x}")
        orig(down, keysym)

    inp.key = key
    return keys


def old_zoom_buttons(img) -> int:
    """67% の画面で、帯の中ほど (100% のときの倍率のボタンがあった所) の明るい画素の数。
    描き直しが遅れると、前の絵 (「+ 開く − 縮小 100% + 拡大」) がここに残る。正しく描けていれば帯の地だけで 0
    (67% の「+ 開く」は 250 より右にある)"""
    return sum(1 for p in img.crop((110, 2, 245, 22)).convert("L").get_flattened_data() if p > 90)


def is_light(img) -> bool:
    """画像の中ほどが白っぽいか (試験用の窓の地は白)。"""
    return min(img.convert("RGB").getpixel((img.width // 2, img.height // 2))) > 200


def read_qr(path: Path) -> list[str]:
    """画像の中の QR コードを読む (スマホのカメラの代わり)。"""
    import zxingcpp
    return [b.text for b in zxingcpp.read_barcodes(Image.open(path)) if b.format == zxingcpp.BarcodeFormat.QRCode]


def shot(dock: td.Dock, name: str) -> None:
    """画面の内側を撮る。窓そのものを撮る (PrintWindow) ので、ほかの窓の後ろにあっても写る。"""
    hwnd = int(tk_get(dock, dock._remote_panel.wm_frame), 16)
    w, h = remote.client_size(hwnd)
    data = remote.compose([(hwnd, 0, 0, w, h)], w, h, 1.0)
    Image.frombuffer("RGB", (w, h), data, "raw", "BGRX").save(OUT / name)


def adopt_only_test_windows(dock: td.Dock) -> None:
    """つないでいる間に開いた窓のうち、試験用の窓だけをタブにする。人がその間に開いた窓 (Cygwin のセットアップ・
    エクスプローラー・ToolKeeper など) まで試験のドックに取り込み、選ばれたタブが変わって試験が時々落ちた"""
    orig = dock.maybe_adopt_remote

    def only_tests(hwnd: int) -> None:
        if td.window_title(hwnd).startswith(("vnc-testapp ", "edit-testapp ")):
            orig(hwnd)

    dock.maybe_adopt_remote = only_tests


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
    adopt_only_test_windows(dock)
    if os.environ.get("CHECK_REMOTE_DUMP"):
        watch_tk(dock)
    saved_clip = remote.clipboard_text()  # 試験でクリップボードを書き換えるので、文字なら最後に戻す
    threading.Thread(target=lambda: (time.sleep(1.0), steps(dock)), daemon=True).start()
    dock.root.mainloop()
    if saved_clip is not None:
        remote.set_clipboard_text(saved_clip)
    ng = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(ng)}/{len(results)} OK")
    sys.exit(1 if ng else 0)


if __name__ == "__main__":
    main()
