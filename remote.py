"""WinTabDock を遠隔 (スマホのブラウザ) から使うための VNC サーバ。標準ライブラリだけで書く (exe に Pillow を入れない)。

ブラウザの側は noVNC。ブラウザの大きさを SetDesktopSize で送ってくるので、ドックをその大きさにする。
試作と、試して分かったことは experiments/vnc_probe.py と docs/DEVELOPMENT.md の「遠隔で使う」。

- 撮る: PrintWindow で窓ごとに描かせ (隠れていても画面の外でも撮れる)、GDI で 1 枚に重ねる (縮めるのも GDI)
- 送る: 変わった行の帯だけ、zlib で縮めて送る (VNC の Zlib 符号)
- 押す・打つ: 窓へのメッセージで送る。手元のマウスとキーボードは動かさない
- 入れるのは、URL の合言葉 (token) が合い、Origin が自分の配ったページのものだけ
"""

from __future__ import annotations

import base64
import ctypes
import hashlib
import io
import mimetypes
import re
import secrets
import socket
import struct
import tarfile
import threading
import time
import urllib.parse
import urllib.request
import zlib
from ctypes import wintypes
from pathlib import Path
from typing import Callable, Protocol

from keysyms import KEYSYM_TO_CODEPOINT

DEFAULT_PORT = 15900  # LPortMan の台帳に登録
NOVNC_VERSION = "1.6.0"
NOVNC_URL = f"https://github.com/novnc/noVNC/archive/refs/tags/v{NOVNC_VERSION}.tar.gz"
NOVNC_SHA256 = "5066103959ef4e9b10f37e5a148627360dd8414e4cf8a7db92bdbd022e728aaa"
FRAME_INTERVAL = 1 / 15  # 撮る間隔の下限 (秒)

# ── Win32 ────────────────────────────────────────────────────────────────────

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
LPARAM = ctypes.c_ssize_t

_u = user32
_u.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, LPARAM]
_u.SendMessageTimeoutW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, LPARAM, wintypes.UINT,
                                   wintypes.UINT, ctypes.c_void_p]
_u.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
_u.GetKeyboardState.argtypes = [ctypes.c_void_p]
_u.SetKeyboardState.argtypes = [ctypes.c_void_p]
_u.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
_u.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
_u.MapWindowPoints.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.POINTER(wintypes.POINT), wintypes.UINT]
_u.ChildWindowFromPointEx.argtypes = [wintypes.HWND, wintypes.POINT, wintypes.UINT]
_u.ChildWindowFromPointEx.restype = wintypes.HWND
_u.PrintWindow.argtypes = [wintypes.HWND, wintypes.HDC, wintypes.UINT]
_u.GetDC.argtypes = [wintypes.HWND]
_u.GetDC.restype = wintypes.HDC
_u.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
_u.FillRect.argtypes = [wintypes.HDC, ctypes.POINTER(wintypes.RECT), wintypes.HBRUSH]
_u.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
_u.MapVirtualKeyW.argtypes = [wintypes.UINT, wintypes.UINT]
_u.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                            wintypes.UINT]
_u.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
_u.MenuItemFromPoint.argtypes = [wintypes.HWND, wintypes.HMENU, wintypes.POINT]
_u.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
_u.MonitorFromWindow.restype = wintypes.HMONITOR
_u.GetMonitorInfoW.argtypes = [wintypes.HMONITOR, ctypes.c_void_p]
gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
gdi32.CreateCompatibleDC.restype = wintypes.HDC
gdi32.CreateDIBSection.argtypes = [wintypes.HDC, ctypes.c_void_p, wintypes.UINT, ctypes.POINTER(ctypes.c_void_p),
                                   wintypes.HANDLE, wintypes.DWORD]
gdi32.CreateDIBSection.restype = wintypes.HBITMAP
gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
gdi32.SelectObject.restype = wintypes.HGDIOBJ
gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
gdi32.DeleteDC.argtypes = [wintypes.HDC]
gdi32.CreateSolidBrush.argtypes = [wintypes.COLORREF]
gdi32.CreateSolidBrush.restype = wintypes.HBRUSH
gdi32.SetStretchBltMode.argtypes = [wintypes.HDC, ctypes.c_int]
gdi32.SetBrushOrgEx.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_void_p]
gdi32.StretchBlt.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                             wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.DWORD]
gdi32.BitBlt.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                         wintypes.HDC, ctypes.c_int, ctypes.c_int, wintypes.DWORD]

PW_CLIENTONLY, PW_RENDERFULLCONTENT = 0x1, 0x2
SRCCOPY, HALFTONE = 0x00CC0020, 4
CWP_SKIPINVISIBLE, CWP_SKIPTRANSPARENT = 0x1, 0x4
SMTO_ABORTIFHUNG = 0x0002
# 押す・打つを相手が処理し終えるのを待つ長さ。時間切れになったものは捨てられ、後からも処理されない。500ms では、
# Tk が絵文字を最初に描くとき (約 1 秒止まる) や縮めた直後に、次の絵文字やボタンを押したことが抜けた。
# 固まった相手は SMTO_ABORTIFHUNG ですぐ諦めるので、長く待つのは「忙しいが動いている」相手だけ
SEND_TIMEOUT_MS = 5000
WM_ACTIVATE, WA_ACTIVE, WM_SETFOCUS = 0x0006, 1, 0x0007
WM_KEYDOWN, WM_KEYUP, WM_CHAR, WM_UNICHAR = 0x0100, 0x0101, 0x0102, 0x0109
WM_MOUSEMOVE, WM_MOUSEWHEEL = 0x0200, 0x020A
# メニューの窓 (#32768) へのメッセージ (文書には無いが、Windows 自身がメニューの窓に使う)。項目の番号を渡す
MN_GETHMENU, MN_SELECTITEM, MN_CANCELMENUS, MN_BUTTONDOWN, MN_BUTTONUP = 0x01E1, 0x01E5, 0x01E6, 0x01ED, 0x01EF
SWP_NOSIZE, SWP_NOZORDER, SWP_NOACTIVATE = 0x0001, 0x0004, 0x0010
BUTTON_MSGS = {1: (0x0201, 0x0202, 0x0001), 2: (0x0207, 0x0208, 0x0010), 4: (0x0204, 0x0205, 0x0002)}  # 左・中・右
DOWN_MSGS = {down for down, _, _ in BUTTON_MSGS.values()}


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
                ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG), ("biYPelsPerMeter", wintypes.LONG),
                ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD)]


class GUITHREADINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("flags", wintypes.DWORD), ("hwndActive", wintypes.HWND),
                ("hwndFocus", wintypes.HWND), ("hwndCapture", wintypes.HWND), ("hwndMenuOwner", wintypes.HWND),
                ("hwndMoveSize", wintypes.HWND), ("hwndCaret", wintypes.HWND), ("rcCaret", wintypes.RECT)]


_u.GetGUIThreadInfo.argtypes = [wintypes.DWORD, ctypes.POINTER(GUITHREADINFO)]


def client_size(hwnd: int) -> tuple[int, int]:
    r = wintypes.RECT()
    _u.GetClientRect(hwnd, ctypes.byref(r))
    return r.right, r.bottom


def client_origin(hwnd: int) -> tuple[int, int]:
    """窓の内側の左上の、画面での位置。"""
    pt = wintypes.POINT(0, 0)
    _u.ClientToScreen(hwnd, ctypes.byref(pt))
    return pt.x, pt.y


def lparam_xy(x: int, y: int) -> int:
    return (y & 0xFFFF) << 16 | (x & 0xFFFF)


def _dib(dc: int, w: int, h: int) -> tuple[int, ctypes.c_void_p]:
    bmi = BITMAPINFOHEADER(ctypes.sizeof(BITMAPINFOHEADER), w, -h, 1, 32, 0, 0, 0, 0, 0, 0)  # 上から下へ・BGRX
    bits = ctypes.c_void_p()
    return gdi32.CreateDIBSection(dc, ctypes.byref(bmi), 0, ctypes.byref(bits), None, 0), bits


_chromium: dict[int, bool] = {}
_EnumProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, LPARAM)
_u.EnumChildWindows.argtypes = [wintypes.HWND, _EnumProc, LPARAM]
_u.EnumWindows.argtypes = [_EnumProc, LPARAM]
_u.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]


def uses_chromium(hwnd: int) -> bool:
    """窓かその子窓が Chromium (Edge・Electron・WebView2) か。窓の種類の名前が Chrome_ で始まる。"""
    if hwnd not in _chromium:
        found = []
        buf = ctypes.create_unicode_buffer(64)

        def cb(h, _):
            _u.GetClassNameW(h, buf, 64)
            if buf.value.startswith("Chrome_"):
                found.append(h)
                return False
            return True

        _u.GetClassNameW(hwnd, buf, 64)
        if not buf.value.startswith("Chrome_"):
            _u.EnumChildWindows(hwnd, _EnumProc(cb), 0)
        else:
            found.append(hwnd)
        _chromium[hwnd] = bool(found)
    return _chromium[hwnd]


def is_black(bgrx: bytes) -> bool:
    """色がすべて 0 か (4 バイト目は使わないバイトで、0 とは限らない)。"""
    n = len(bgrx) // 4
    return all(bgrx[i::4].count(0) == n for i in range(3))


# 重ねる 1 枚: (窓, 左上の x, y, 幅, 高さ)。座標は窓の実際の画素で、見せる範囲の左上が (0, 0)
Layer = tuple[int, int, int, int, int]


def compose(layers: list[Layer], fb_w: int, fb_h: int, scale: float) -> bytes:
    """窓を下から順に撮って scale 倍で重ね、fb_w x fb_h の BGRX (上から下へ) にする。余りは灰色。"""
    screen = _u.GetDC(None)
    out_dc = gdi32.CreateCompatibleDC(screen)
    tmp_dc = gdi32.CreateCompatibleDC(screen)
    out_bmp, out_bits = _dib(out_dc, fb_w, fb_h)
    old_out = gdi32.SelectObject(out_dc, out_bmp)
    try:
        brush = gdi32.CreateSolidBrush(0x404040)
        _u.FillRect(out_dc, ctypes.byref(wintypes.RECT(0, 0, fb_w, fb_h)), brush)
        gdi32.DeleteObject(brush)
        gdi32.SetStretchBltMode(out_dc, HALFTONE)
        gdi32.SetBrushOrgEx(out_dc, 0, 0, None)
        for hwnd, x, y, w, h in layers:
            if w <= 0 or h <= 0:
                continue
            bmp, bits = _dib(tmp_dc, w, h)
            old = gdi32.SelectObject(tmp_dc, bmp)
            try:
                # PW_RENDERFULLCONTENT は Chromium (WebView2) の中身も撮れるが、画面の外で出した Tk の窓
                # (遠隔でつないでいる間にタブを切り替えた窓) は、真っ黒や一部だけ黒く写った。Chromium の窓にだけ使い、
                # ほかは窓に描かせる普通の撮り方にする。それでも真っ黒なら、もう一方で撮り直す
                full = uses_chromium(hwnd)
                _u.PrintWindow(hwnd, tmp_dc, PW_CLIENTONLY | (PW_RENDERFULLCONTENT if full else 0))
                if is_black(ctypes.string_at(bits, w * h * 4)):
                    _u.PrintWindow(hwnd, tmp_dc, PW_CLIENTONLY | (0 if full else PW_RENDERFULLCONTENT))
                if scale == 1:
                    gdi32.BitBlt(out_dc, x, y, w, h, tmp_dc, 0, 0, SRCCOPY)
                else:
                    gdi32.StretchBlt(out_dc, round(x * scale), round(y * scale), max(1, round(w * scale)),
                                     max(1, round(h * scale)), tmp_dc, 0, 0, w, h, SRCCOPY)
            finally:
                gdi32.SelectObject(tmp_dc, old)
                gdi32.DeleteObject(bmp)
        return ctypes.string_at(out_bits, fb_w * fb_h * 4)
    finally:
        gdi32.SelectObject(out_dc, old_out)
        gdi32.DeleteObject(out_bmp)
        gdi32.DeleteDC(out_dc)
        gdi32.DeleteDC(tmp_dc)
        _u.ReleaseDC(None, screen)


# ── 見せるもの (ドックが用意する) ─────────────────────────────────────────────

class Source(Protocol):
    """遠隔に見せるもの。ドックが実装する。layers / extent は別のスレッドから呼ばれる。"""

    def layers(self) -> list[Layer]: ...  # 下から順に。最初はドック
    def extent(self) -> tuple[int, int]: ...  # 見せる範囲の大きさ (窓の実際の画素)
    def set_client_size(self, w: int, h: int) -> None: ...  # ドックの内側をこの大きさにする (終わるまで待つ)
    def key_window(self) -> int: ...  # キーの宛先 (選んでいるツール)。無ければ 0
    def click_allowed(self, top: int, child: int, mask: int) -> bool: ...  # 押してよい所か
    def connected(self, on: bool) -> None: ...  # 最初の接続が来た / 最後の接続が切れた
    def title(self) -> str: ...


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT), ("rcWork", wintypes.RECT),
                ("dwFlags", wintypes.DWORD)]


def window_rect(hwnd: int) -> tuple[int, int, int, int]:
    r = wintypes.RECT()
    _u.GetWindowRect(hwnd, ctypes.byref(r))
    return r.left, r.top, r.right, r.bottom


def send_result(hwnd: int, msg: int, wp: int = 0, lp: int = 0) -> int:
    res = ctypes.c_size_t()
    _u.SendMessageTimeoutW(hwnd, msg, wp, lp, SMTO_ABORTIFHUNG, SEND_TIMEOUT_MS, ctypes.addressof(res))
    return res.value


class Menus:
    """選んでいるツールが開いたメニュー (#32768 の窓)。メニューはツールの窓とは別の窓で、持ち主 (owner) も無い。
    ドックは画面の外にあるので、Windows はメニューを画面の中へ寄せて出す (遠隔からは見えず、PC の画面に出ていた)。
    見つけたら、ドックの横 (画面の外) の、出るはずだった所へ移して、遠隔の画面に重ねる。"""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.placed: dict[int, tuple[int, int]] = {}  # 移したメニューの窓 → 移した先 (画面の座標)
        self.anchor = (0, 0)  # 最後に押した所 (見せる範囲の座標)。メニューの横の位置にする

    @staticmethod
    def find(top: int) -> list[int]:
        """top と同じスレッドの、見えているメニューの窓 (奥から。最初が一番上の段)。"""
        tid = _u.GetWindowThreadProcessId(top, None)
        found: list[int] = []
        buf = ctypes.create_unicode_buffer(16)

        def cb(h, _):
            _u.GetClassNameW(h, buf, 16)
            if buf.value == "#32768" and _u.IsWindowVisible(h) and _u.GetWindowThreadProcessId(h, None) == tid:
                found.append(h)
            return True

        _u.EnumWindows(_EnumProc(cb), 0)
        return found[::-1]

    def layers(self, top: int, dock: int, view_w: float, view_h: float) -> list[Layer]:
        with self.lock:
            menus = self.find(top)
            self.placed = {h: p for h, p in self.placed.items() if h in menus}  # 閉じた窓は使い回される
            ox, oy = client_origin(dock)
            out: list[Layer] = []
            parent = None  # 一つ上の段の、窓の範囲 (見せる範囲の座標)
            for h in menus:
                l, t, r, b = window_rect(h)
                if self.placed.get(h) != (l, t):
                    x, y = self._place(h, (l - ox, t - oy, r - l, b - t), b, parent, view_w, view_h)
                    _u.SetWindowPos(h, None, ox + x, oy + y, 0, 0, SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE)
                    self.placed[h] = (ox + x, oy + y)
                    l, t, r, b = window_rect(h)
                parent = (l - ox, t - oy, r - l, b - t)
                cx, cy = client_origin(h)
                out.append((h, cx - ox, cy - oy, *client_size(h)))
            return out

    def _place(self, hwnd: int, rect: tuple[int, int, int, int], screen_bottom: int,
               parent: tuple[int, int, int, int] | None, view_w: float, view_h: float) -> tuple[int, int]:
        """メニューを置く所 (見せる範囲の座標)。縦は Windows が出した位置 (ドックは画面と同じ高さにあるので、
        ツールが頼んだ位置のまま)。ただし画面の下で寄せられていたら、押した所にする。横は画面の外へ出せないので
        寄せられている。一番上の段は押した所、下の段は上の段の右 (入らなければ左)"""
        x, y, w, h = rect
        if parent is None:
            info = MONITORINFO(cbSize=ctypes.sizeof(MONITORINFO))
            _u.GetMonitorInfoW(_u.MonitorFromWindow(hwnd, 2), ctypes.byref(info))  # MONITOR_DEFAULTTONEAREST
            if y < 0 or screen_bottom >= min(info.rcWork.bottom, info.rcMonitor.bottom) - 1:
                y = round(self.anchor[1])
            x = round(self.anchor[0])
        else:
            px, _, pw, _ = parent
            x = px + pw - 3  # Windows も少し重ねる
            if x + w > view_w:
                x = px - w + 3
        x = max(0, min(x, round(view_w) - w))
        y = max(0, min(y, round(view_h) - h))
        return x, y

    def item_at(self, hwnd: int, x: int, y: int) -> int:
        """メニューの内側の (x, y) にある項目の番号。無ければ -1。"""
        hmenu = send_result(hwnd, MN_GETHMENU)
        cx, cy = client_origin(hwnd)
        return _u.MenuItemFromPoint(None, hmenu, wintypes.POINT(cx + x, cy + y)) if hmenu else -1

    def cancel(self, top: int) -> None:
        """開いているメニューを全部閉じる (一番上の段に MN_CANCELMENUS。下の段の窓に Esc を置いても効かなかった)。"""
        menus = self.find(top)
        if menus:
            _u.SendMessageTimeoutW(menus[0], MN_CANCELMENUS, 0, 0, SMTO_ABORTIFHUNG, SEND_TIMEOUT_MS, None)


class View:
    """Source を VNC の画面にする。VNC の画面の画素 = 窓の画素 x scale。

    つないだ側が大きさを決めたら (SetDesktopSize) ドックをその大きさ ÷ 表示倍率 (zoom) にし、zoom 倍で見せる。
    zoom が 1 未満なら、ドックを大きく作って縮めて見せる (横に多く入る)。1 より大きければ小さく作って広げて見せる (文字が大きい)。
    ツールが最小の幅などで収まらなければ、さらに縮めて収め、縮めた分だけドックを縦に伸ばす
    (PortWho は幅 680、LPortMan は 760 より狭くならない)。"""

    def __init__(self, source: Source, zoom: float = 1.0):
        self.source = source
        self.requested: tuple[int, int] | None = None
        self.zoom = zoom
        self.scale = 1.0
        self.lock = threading.Lock()
        self.menus = Menus()
        self.input = WindowInput(self)

    def layers(self) -> list[Layer]:
        """見せる窓。Source の窓に、選んでいるツールが開いたメニューを足す (メニューは画面の外の出る所へ移す)。"""
        layers = self.source.layers()
        top = self.source.key_window()
        if top and layers:
            w, h = self.fb_size()
            layers += self.menus.layers(top, layers[0][0], w / self.scale, h / self.scale)
        return layers

    def _fit(self) -> float:
        w, h = self.source.extent()
        if not self.requested or w <= 0 or h <= 0:
            return 1.0
        return min(self.zoom, self.requested[0] / w, self.requested[1] / h)

    def fb_size(self) -> tuple[int, int]:
        if self.requested:
            return self.requested
        w, h = self.source.extent()
        return max(1, w), max(1, h)

    def resize(self, w: int, h: int) -> None:
        with self.lock:
            self.requested = (w, h)
            lw, lh = round(w / self.zoom), round(h / self.zoom)
            self.source.set_client_size(lw, lh)
            s = self._fit()
            if s < self.zoom:
                # ツールが収まらず、さらに縮めるなら、その倍率で縦を使い切る高さにする。幅はツールに揃える
                # (タブの帯もツールの幅まで伸ばす)
                cw, _ = self.source.extent()
                self.source.set_client_size(max(lw, cw), round(h / s))
            self.scale = self._fit()

    def frame(self) -> bytes:
        with self.lock:
            self.scale = self._fit()
            w, h = self.fb_size()
            return compose(self.layers(), w, h, self.scale)

    def hit(self, x: int, y: int) -> tuple[int, int, int] | None:
        """VNC の画面の (x, y) にある窓と、その内側の座標 (上の窓から探す)。"""
        cx, cy = x / self.scale, y / self.scale
        for hwnd, lx, ly, lw, lh in reversed(self.layers()):
            if lx <= cx < lx + lw and ly <= cy < ly + lh:
                return hwnd, int(cx - lx), int(cy - ly)
        return None


class WindowInput:
    """押す・打つを窓へのメッセージで送る。手元のマウスとキーボードは動かさない。

    Tk は、押したかをマウスの状態 (GetKeyState) で見るので、相手のスレッドと入力の状態を共有して書き換えてから送る。
    押している間は移動を送り続ける (Tk は本物のカーソルの位置を見に行き、そこに自分が無いと「出た」ことにする)。
    押すたびに WM_ACTIVATE と WM_SETFOCUS を送る (Tk は前面でないと届いた文字を捨てる)。"""

    def __init__(self, view: View):
        self.view = view
        self.lock = threading.Lock()
        self.buttons = 0
        self.last = (0, 0)
        self.press: tuple[int, int] | None = None  # 押している間の宛先 (外側の窓, 子窓)
        self.key_child = 0
        self.holding = False
        self.blocked = False  # 押してはいけない所を押した。離すまで何も送らない
        self.high_surrogate = 0  # 絵文字の代理対の前半 (後半が来るまで控える)
        self.send_failures: list = []  # 渡せなかったもの (メッセージ, 値, GetLastError)
        self.menu_press = False  # メニューの上で押した (離した時に項目を選ぶ)

    def pointer(self, mask: int, x: int, y: int) -> None:
        with self.lock:
            self._pointer(mask, x, y)
            self.buttons, self.last = mask, (x, y)
            if mask & 7 and not self.holding:
                self.holding = True
                threading.Thread(target=self._hold, daemon=True).start()

    def _hold(self) -> None:
        while True:
            time.sleep(0.02)
            with self.lock:
                if not self.buttons & 7:
                    self.holding = False
                    return
                self._pointer(self.buttons, *self.last)

    def _pointer(self, mask: int, x: int, y: int) -> None:
        if mask & ~self.buttons & 7:
            self.view.menus.anchor = (x / self.view.scale, y / self.view.scale)
        tool = self.view.source.key_window()
        if tool and (self.menu_press or Menus.find(tool)):
            self._menu_pointer(tool, mask, x, y)
            return
        if self.buttons & 7 and self.press:
            top, child = self.press
            hit = self.view.hit(x, y)
            # 押したまま別の窓の上に出ても、押した子窓に送り続ける (ドラッグ)
            lx, ly = (hit[1], hit[2]) if hit and hit[0] == top else (0, 0)
            pt = wintypes.POINT(lx, ly)
            _u.MapWindowPoints(top, child, ctypes.byref(pt), 1)
            cx, cy = pt.x, pt.y
        else:
            hit = self.view.hit(x, y)
            if not hit:
                return
            top, lx, ly = hit
            child, cx, cy = child_at(top, lx, ly)
        pressing = mask & ~self.buttons & 7
        if pressing and not self.view.source.click_allowed(top, child, mask):
            self.blocked = True
        if self.blocked:
            # 断った押し込みは、離すまで何も送らない。押した状態の移動を送るだけでも、Tk はマウスの状態から
            # 「押された」ことにする (「+ 起動」のメニューが遠隔から開いてしまった)
            if not mask & 7:
                self.blocked = False
            return
        mk = sum(BUTTON_MSGS[b][2] for b in BUTTON_MSGS if mask & b)
        msgs = [(WM_MOUSEMOVE, mk, lparam_xy(cx, cy))]
        for bit, (down, up, _) in BUTTON_MSGS.items():
            if mask & bit and not self.buttons & bit:
                self.press = (top, child)
                if top == self.view.source.key_window():
                    self.key_child = child
                msgs.append((down, mk, lparam_xy(cx, cy)))
            elif self.buttons & bit and not mask & bit:
                msgs.append((up, mk, lparam_xy(cx, cy)))
        if not mask & 7:
            self.press = None
        for bit, delta in ((8, 120), (16, -120)):  # ホイールは押した瞬間だけ。座標は画面の座標
            if mask & bit and not self.buttons & bit:
                sx, sy = client_origin(top)
                msgs.append((WM_MOUSEWHEEL, (delta & 0xFFFF) << 16 | mk, lparam_xy(sx + cx, sy + cy)))
        tid = _u.GetWindowThreadProcessId(top, None)
        me = kernel32.GetCurrentThreadId()
        attached = _u.AttachThreadInput(me, tid, True)
        try:
            if attached:
                state = (ctypes.c_ubyte * 256)()
                _u.GetKeyboardState(state)
                for bit, vk in ((1, 0x01), (2, 0x04), (4, 0x02)):  # VK_LBUTTON / VK_MBUTTON / VK_RBUTTON
                    state[vk] = 0x80 if mask & bit else 0
                _u.SetKeyboardState(state)
            menu_opened = False
            for msg, wp, lp in msgs:
                if msg in DOWN_MSGS:
                    menu_opened = self._send_press(top, child, msg, wp, lp)
                    if menu_opened:
                        break
                else:
                    _u.SendMessageTimeoutW(child, msg, wp, lp, SMTO_ABORTIFHUNG, SEND_TIMEOUT_MS, None)
        finally:
            if attached:
                _u.AttachThreadInput(me, tid, False)
        if pressing and not menu_opened:
            _u.SendMessageTimeoutW(top, WM_ACTIVATE, WA_ACTIVE, 0, SMTO_ABORTIFHUNG, SEND_TIMEOUT_MS, None)
            _u.SendMessageTimeoutW(child, WM_SETFOCUS, 0, 0, SMTO_ABORTIFHUNG, SEND_TIMEOUT_MS, None)

    def _send_press(self, top: int, hwnd: int, msg: int, wp: int, lp: int) -> bool:
        """押した知らせを渡して、処理し終えるのを待つ。押した時にメニューを出すツール (Mery のメニューの帯) は、
        メニューを閉じるまで処理から戻らないので、待ち続けると遠隔からメニューを選べない (受け取りが 5 秒止まる)。
        メニューが開いたら待つのをやめる。開いたら True"""
        done = threading.Event()

        def run() -> None:
            _u.SendMessageTimeoutW(hwnd, msg, wp, lp, SMTO_ABORTIFHUNG, SEND_TIMEOUT_MS, None)
            done.set()

        threading.Thread(target=run, daemon=True).start()
        while not done.wait(0.02):
            if Menus.find(top):
                self.view.layers()  # すぐ画面の外へ移す (PC の画面に出ている間を短くする)
                return True
        return False

    @staticmethod
    def _set_button_state(top: int, mask: int) -> None:
        """top のスレッドの入力の状態の、マウスのボタンを mask にする。"""
        tid = _u.GetWindowThreadProcessId(top, None)
        me = kernel32.GetCurrentThreadId()
        if _u.AttachThreadInput(me, tid, True):
            try:
                state = (ctypes.c_ubyte * 256)()
                _u.GetKeyboardState(state)
                for bit, vk in ((1, 0x01), (2, 0x04), (4, 0x02)):  # VK_LBUTTON / VK_MBUTTON / VK_RBUTTON
                    state[vk] = 0x80 if mask & bit else 0
                _u.SetKeyboardState(state)
            finally:
                _u.AttachThreadInput(me, tid, False)

    def _menu_pointer(self, tool: int, mask: int, x: int, y: int) -> None:
        """メニューを開いている間の押す・動かす。ツールはメニューの処理の中で本物のマウスを見ているので、窓への
        マウスのメッセージは効かない。メニューの窓に、項目の番号で「選ぶ」「押す」「離す」を渡す"""
        pressing = mask & ~self.buttons & 7
        releasing = self.buttons & ~mask & 7
        hit = self.view.hit(x, y)  # メニューを探して移すのもここ
        menu = hit[0] if hit and hit[0] in self.view.menus.placed else 0
        if not mask & 7:
            self.press = None
        if releasing:
            # メニューを開いた押し込みで、ツールの入力の状態を「押している」にしたままになる。メニューは押したまま
            # (ドラッグ中) だと思い、キー (↓ Enter) を受け付けなかった
            self._set_button_state(tool, mask)
            if releasing & 1 and not self.menu_press:
                menus = Menus.find(tool)
                if menus:
                    # メニューは押したまま開くと、受け取りの列に「離した」が来るまで待っている。項目の無い所 (遠く) で離す
                    _u.PostMessageW(menus[0], BUTTON_MSGS[1][1], 0, lparam_xy(-30000, -30000))
        if pressing:
            self.menu_press = bool(menu)
            if not menu:
                # メニューの外を押すと閉じる。Windows と同じく、その押し込みはツールに渡さない (離すまで何も送らない)
                self.view.menus.cancel(tool)
                self.blocked = True
                return
        if self.blocked:
            if not mask & 7:
                self.blocked = False
            return
        if menu:
            idx = self.view.menus.item_at(menu, hit[1], hit[2])

            def send(m: int) -> None:
                _u.SendMessageTimeoutW(menu, m, idx & 0xFFFFFFFF, 0, SMTO_ABORTIFHUNG, SEND_TIMEOUT_MS, None)

            send(MN_SELECTITEM)
            # 項目を押して離すと選ぶ (下の段を持つ項目は押すと開く)。開いた時の押し込み (帯の上) を離しても選ばない
            if idx >= 0 and pressing & 1:
                send(MN_BUTTONDOWN)
            if idx >= 0 and releasing & 1 and self.menu_press:
                send(MN_BUTTONUP)
        if not mask & 7:
            self.menu_press = False

    def key(self, down: bool, keysym: int) -> None:
        vk = SPECIAL_KEYS.get(keysym)
        ch = None if vk else keysym_char(keysym)
        top = self.view.source.key_window()
        if (vk is None and ch is None) or not top:
            return
        menus = Menus.find(top)
        if menus:
            # メニューを開いている間、ツールはメニューの処理の中で受け取りの列からキーを読む (SendMessage では届かない)。
            # 一番上の段の窓に置く (下の段の窓に置いても効かなかった)。↓ ↑ → ← Enter Esc と、項目の頭文字
            if vk and vk not in (0x10, 0x11, 0x12):
                lp = 1 | _u.MapVirtualKeyW(vk, 0) << 16
                _u.PostMessageW(menus[0], WM_KEYDOWN if down else WM_KEYUP, vk, lp if down else lp | 0xC0000000)
            elif ch and down:
                _u.PostMessageW(menus[0], WM_CHAR, ord(ch), 1)
            return
        dest = focus_of(top) or self.key_child or top

        # 打つものは、相手の受け取りの列に置かず (PostMessage)、1 つずつ渡して処理し終えるのを待つ (SendMessage)。
        # 列に置くと順番が崩れた。BackSpace の WM_KEYDOWN は、相手が受け取って翻訳した時に WM_CHAR(8) が列の後ろに作られ、
        # 先に置いた文字に追い越される。スマホの日本語の入力は、変換中の「に ほ ん」を送ってから、確定で BackSpace 3 回と
        # 「日 本」を送ってくるので、Mery (WM_CHAR(8) で消す) では「にほん日本」から 3 文字消えて「にほ」が残った。Tk は
        # WM_KEYDOWN の時に列から次の WM_CHAR を拾ってそのキーの文字にするので、「日」を拾って BackSpace と一緒に消した
        def send(msg: int, wp: int, lp: int) -> None:
            if not _u.SendMessageTimeoutW(dest, msg, wp, lp, SMTO_ABORTIFHUNG, SEND_TIMEOUT_MS, None):
                self.send_failures = self.send_failures[-19:] + [(hex(msg), hex(wp), ctypes.get_last_error())]

        if vk:
            if vk in (0x10, 0x11, 0x12):
                return  # 修飾キーは窓へのメッセージでは効かない (相手は本物のキーボードの状態を読む)
            lp = 1 | _u.MapVirtualKeyW(vk, 0) << 16
            send(WM_KEYDOWN if down else WM_KEYUP, vk, lp if down else lp | 0xC0000000)
            if down and vk in CHAR_KEYS and not is_tk(dest):
                # 渡したキーは翻訳を通らないので、文字を生むキー (BackSpace・Tab・Enter・Esc) はその文字も渡す。
                # Tk には渡さない (Tk は WM_KEYDOWN だけで動き、Text 部品では Tab や Enter が二重に入りうる)
                send(WM_CHAR, vk, lp)
        elif down:
            # noVNC は絵文字 (U+1F366 など) を、1 つの keysym でなく、代理対の片方ずつ 2 回に分けて送ってくる。
            # 前半を控えて、後半が来たら 1 文字にする (片方だけの文字を encode しようとして例外になり、接続ごと切れたことがある)
            cp = ord(ch)
            if 0xD800 <= cp <= 0xDBFF:
                self.high_surrogate = cp
                return
            if 0xDC00 <= cp <= 0xDFFF:
                if not self.high_surrogate:
                    return
                cp = 0x10000 + ((self.high_surrogate - 0xD800) << 10) + (cp - 0xDC00)
            self.high_surrogate = 0
            if cp <= 0xFFFF:
                send(WM_CHAR, cp, 1)
            elif is_tk(dest):
                # Tk は代理対の 2 つを別々に渡されると、つなげそこねた (前半が落ちて別の文字になった)。1 文字まるごと渡す
                send(WM_UNICHAR, cp, 1)
            else:
                # WM_CHAR は UTF-16 の 1 単位ずつ。代理対の 2 つに分けて渡す (Windows 自身もそうする。Mery で確かめた)
                send(WM_CHAR, 0xD800 | (cp - 0x10000) >> 10, 1)
                send(WM_CHAR, 0xDC00 | (cp - 0x10000) & 0x3FF, 1)


def child_at(hwnd: int, x: int, y: int) -> tuple[int, int, int]:
    """内側の (x, y) にある一番奥の子窓と、その子窓の中の座標。"""
    pt = wintypes.POINT(x, y)
    while True:
        child = _u.ChildWindowFromPointEx(hwnd, pt, CWP_SKIPINVISIBLE | CWP_SKIPTRANSPARENT)
        if not child or child == hwnd:
            return hwnd, pt.x, pt.y
        _u.MapWindowPoints(hwnd, child, ctypes.byref(pt), 1)
        hwnd = child


def focus_of(hwnd: int) -> int:
    info = GUITHREADINFO(cbSize=ctypes.sizeof(GUITHREADINFO))
    if _u.GetGUIThreadInfo(_u.GetWindowThreadProcessId(hwnd, None), ctypes.byref(info)):
        return info.hwndFocus or 0
    return 0


SPECIAL_KEYS = {  # X11 の keysym → 仮想キー
    0xFF08: 0x08, 0xFF09: 0x09, 0xFF0D: 0x0D, 0xFF8D: 0x0D, 0xFF1B: 0x1B, 0xFFFF: 0x2E, 0xFF63: 0x2D,
    0xFF50: 0x24, 0xFF57: 0x23, 0xFF55: 0x21, 0xFF56: 0x22,
    0xFF51: 0x25, 0xFF52: 0x26, 0xFF53: 0x27, 0xFF54: 0x28,
    0xFFE1: 0x10, 0xFFE2: 0x10, 0xFFE3: 0x11, 0xFFE4: 0x11, 0xFFE9: 0x12, 0xFFEA: 0x12,
    **{0xFFBE + i: 0x70 + i for i in range(12)},  # F1-F12
}


CHAR_KEYS = {0x08, 0x09, 0x0D, 0x1B}  # 文字を生む仮想キー (BackSpace・Tab・Enter・Esc)。文字の値は仮想キーと同じ


def is_tk(hwnd: int) -> bool:
    """Tk の窓か (窓の種類の名前が TkChild・TkTopLevel など)。"""
    buf = ctypes.create_unicode_buffer(32)
    _u.GetClassNameW(hwnd, buf, 32)
    return buf.value.startswith("Tk")


def keysym_char(keysym: int) -> str | None:
    if 0x20 <= keysym <= 0x7E or 0xA0 <= keysym <= 0xFF:
        return chr(keysym)
    if keysym & 0xFF000000 == 0x01000000:  # Unicode の keysym (noVNC はスマホの日本語の確定文字をこれで送る)
        return chr(keysym & 0xFFFFFF)
    cp = KEYSYM_TO_CODEPOINT.get(keysym)  # カタカナ・「。」「ー」などは X の古い keysym で届く (ニ = 0x04C6)
    return chr(cp) if cp else None


# ── 通り道 (WebSocket) ────────────────────────────────────────────────────────

class Closed(Exception):
    pass


class WebSocketStream:
    def __init__(self, sock: socket.socket):
        self.sock = sock
        self.buf = bytearray()

    def _raw(self, n: int) -> bytes:
        out = bytearray()
        while len(out) < n:
            chunk = self.sock.recv(n - len(out))
            if not chunk:
                raise Closed()
            out += chunk
        return bytes(out)

    def recv(self, n: int) -> bytes:
        while len(self.buf) < n:
            b0, b1 = self._raw(2)
            length = b1 & 0x7F
            if length == 126:
                length = struct.unpack(">H", self._raw(2))[0]
            elif length == 127:
                length = struct.unpack(">Q", self._raw(8))[0]
            mask = self._raw(4) if b1 & 0x80 else b"\0\0\0\0"
            data = self._raw(length)
            if length:  # 覆い (mask) を外す。整数にして一度に XOR する
                key = (mask * (length // 4 + 1))[:length]
                data = (int.from_bytes(data, "big") ^ int.from_bytes(key, "big")).to_bytes(length, "big")
            opcode = b0 & 0x0F
            if opcode == 8:
                raise Closed()
            if opcode == 9:
                self._frame(0x8A, data)
            elif opcode in (0, 1, 2):
                self.buf += data
        out = bytes(self.buf[:n])
        del self.buf[:n]
        return out

    def _frame(self, head: int, data: bytes) -> None:
        n = len(data)
        hdr = bytes([head, n]) if n < 126 else bytes([head, 126]) + struct.pack(">H", n) if n < 65536 \
            else bytes([head, 127]) + struct.pack(">Q", n)
        self.sock.sendall(hdr + data)

    def send(self, data: bytes) -> None:
        self._frame(0x82, data)


# ── RFB (VNC の手順) ──────────────────────────────────────────────────────────

ENC_RAW, ENC_ZLIB = 0, 6
PSEUDO_DESKTOP_SIZE, PSEUDO_EXT_DESKTOP_SIZE = -223, -308


class Session:
    """1 つの接続。受けるスレッドと、画面を送るスレッドの 2 本で動く。"""

    def __init__(self, stream: WebSocketStream, server: "Server", name: str):
        self.s = stream
        self.server = server
        self.view = server.view
        self.name = name
        self.send_lock = threading.Lock()
        self.cond = threading.Condition()
        self.request: bool | None = None  # まだ答えていない更新の要求 (incremental か)
        self.encodings: set[int] = set()
        self.pixfmt = (32, 24, 0, 1, 255, 255, 255, 16, 8, 0)
        self.fb = self.view.fb_size()
        self.last: bytes | None = None
        self.resize_reply: int | None = None
        self.announce = False
        self.closed = False
        self.zlib = zlib.compressobj(6)

    def run(self) -> None:
        try:
            self._handshake()
            threading.Thread(target=self._updater, daemon=True).start()
            self._reader()
        except (Closed, ConnectionError, OSError, ValueError):
            pass
        finally:
            self.closed = True
            with self.cond:
                self.cond.notify_all()
            self.server.log(f"{self.name}: 切れた")

    def _send(self, data: bytes) -> None:
        with self.send_lock:
            self.s.send(data)

    def _handshake(self) -> None:
        self.s.send(b"RFB 003.008\n")
        ver = self.s.recv(12)
        if ver[:11] == b"RFB 003.003":
            self.s.send(struct.pack(">I", 1))
        else:
            self.s.send(b"\x01\x01")  # 認証の種類は「なし」だけ (入口で合言葉を確かめている)
            self.s.recv(1)
            self.s.send(struct.pack(">I", 0))
        self.s.recv(1)  # ClientInit
        w, h = self.fb
        name = self.view.source.title().encode("utf-8")
        self.s.send(struct.pack(">HH", w, h) + struct.pack(">BBBBHHHBBB3x", *self.pixfmt)
                    + struct.pack(">I", len(name)) + name)
        self.server.log(f"{self.name}: つながった ({w}x{h})")

    def _input(self, fn, *args) -> None:
        """押す・打つを窓に送る。送るのに失敗しても接続は切らず、記録に残す (絵文字で切れたことがあった)。"""
        try:
            fn(*args)
        except Exception as e:  # noqa: BLE001
            self.server.log(f"{self.name}: 入力を送れなかった {fn.__name__}{args}: {e!r}")

    def _reader(self) -> None:
        r = self.s.recv
        while True:
            t = r(1)[0]
            if t == 0:  # SetPixelFormat
                self.pixfmt = struct.unpack(">3xBBBBHHHBBB3x", r(19))
            elif t == 2:  # SetEncodings
                n = struct.unpack(">xH", r(3))[0]
                encs = set(struct.unpack(f">{n}i", r(4 * n)))
                if PSEUDO_EXT_DESKTOP_SIZE in encs and PSEUDO_EXT_DESKTOP_SIZE not in self.encodings:
                    with self.cond:
                        self.announce = True  # noVNC はこれを一度受けてから大きさを頼んでくる
                        self.cond.notify_all()
                self.encodings = encs
            elif t == 3:  # FramebufferUpdateRequest
                inc = r(9)[0]
                with self.cond:
                    self.request = bool(inc) and (self.request is None or self.request)
                    self.cond.notify_all()
            elif t == 4:  # KeyEvent
                down, key = struct.unpack(">B2xI", r(7))
                self._input(self.view.input.key, bool(down), key)
            elif t == 5:  # PointerEvent
                mask, x, y = struct.unpack(">BHH", r(5))
                self._input(self.view.input.pointer, mask, x, y)
            elif t == 6:  # ClientCutText (読み捨てる)
                n = struct.unpack(">3xi", r(7))[0]
                r(abs(n))
            elif t == 251:  # SetDesktopSize
                w, h, n = struct.unpack(">xHHBx", r(7))
                r(16 * n)
                if self.view.requested == (w, h) == self.fb:
                    # 今と同じ大きさ。受け付けたと返すと noVNC は表示を作り直し、その拍子に端数のある画面
                    # (devicePixelRatio 1.75 で幅 411.43 など) の大きさの監視が反応して、また同じ大きさを頼んでくる
                    # (実機で 1 秒に 10 回、止まらなかった)。「変えない」(状態 1) と返すと止まる
                    status = 1
                else:
                    self.server.log(f"{self.name}: 大きさの要求 {w}x{h}")
                    self.view.resize(w, h)
                    status = 0
                with self.cond:
                    self.resize_reply = status
                    self.cond.notify_all()
            else:
                raise Closed(f"知らないメッセージ {t}")

    def _updater(self) -> None:
        try:
            self._update_loop()
        except (OSError, Closed):
            self.closed = True  # 接続が切れた (止めたときに、送る途中のことがある)

    def _update_loop(self) -> None:
        next_at = 0.0
        while not self.closed:
            with self.cond:
                while not self.closed and self.request is None and self.resize_reply is None and not self.announce:
                    self.cond.wait(0.5)
                if self.closed:
                    return
            time.sleep(max(0.0, next_at - time.monotonic()))
            next_at = time.monotonic() + FRAME_INTERVAL
            fb = self.view.fb_size()
            with self.cond:
                reply, self.resize_reply = self.resize_reply, None
                announce, self.announce = self.announce, False
            if fb != self.fb or reply is not None or announce:
                self._send_size(fb, reply)
                if fb != self.fb or reply == 0:
                    self.fb, self.last = fb, None
                continue
            with self.cond:
                incremental = self.request
            if incremental is None:
                continue
            try:
                frame = self.view.frame()
            except OSError:
                continue
            w, h = self.fb
            if len(frame) != w * h * 4:
                continue  # 大きさが変わる途中
            y0, y1 = 0, h
            if incremental and self.last is not None:
                stride = w * 4
                y0 = next((y for y in range(h) if frame[y * stride:(y + 1) * stride] != self.last[y * stride:(y + 1) * stride]), h)
                if y0 == h:
                    continue  # 変わっていない。要求は残したまま、次を撮る
                y1 = next(y for y in range(h, y0, -1) if frame[(y - 1) * stride:y * stride] != self.last[(y - 1) * stride:y * stride])
            with self.cond:
                self.request = None
            self.last = frame
            self._send_rows(frame, w, y0, y1)

    def _send_size(self, fb: tuple[int, int], reply: int | None) -> None:
        w, h = fb
        if PSEUDO_EXT_DESKTOP_SIZE in self.encodings:
            reason, status = (1, reply) if reply is not None else (0, 0)
            body = struct.pack(">HHHHi", reason, status, w, h, PSEUDO_EXT_DESKTOP_SIZE) + struct.pack(">B3x", 1) \
                + struct.pack(">IHHHHI", 0, 0, 0, w, h, 0)
        elif PSEUDO_DESKTOP_SIZE in self.encodings:
            body = struct.pack(">HHHHi", 0, 0, w, h, PSEUDO_DESKTOP_SIZE)
        else:
            return
        self._send(struct.pack(">BxH", 0, 1) + body)

    def _pixels(self, bgrx: bytes) -> bytes:
        """BGRX をつないだ側の画素の形にする (32 ビットで、色が 1 バイトずつに収まる形だけ)。"""
        bpp, _depth, big, _true, _rm, _gm, _bm, rs, gs, bs = self.pixfmt
        if bpp != 32 or rs % 8 or gs % 8 or bs % 8:
            raise Closed(f"扱えない画素の形 {self.pixfmt}")
        if (rs, gs, bs, big) == (16, 8, 0, 0):
            return bgrx
        out = bytearray(len(bgrx))
        pos = (lambda s: 3 - s // 8) if big else (lambda s: s // 8)
        out[pos(rs)::4] = bgrx[2::4]
        out[pos(gs)::4] = bgrx[1::4]
        out[pos(bs)::4] = bgrx[0::4]
        return bytes(out)

    def _send_rows(self, frame: bytes, w: int, y0: int, y1: int) -> None:
        pixels = self._pixels(frame[y0 * w * 4:y1 * w * 4])
        if ENC_ZLIB in self.encodings:
            data = self.zlib.compress(pixels) + self.zlib.flush(zlib.Z_SYNC_FLUSH)
            body = struct.pack(">HHHHi", 0, y0, w, y1 - y0, ENC_ZLIB) + struct.pack(">I", len(data)) + data
        else:
            body = struct.pack(">HHHHi", 0, y0, w, y1 - y0, ENC_RAW) + pixels
        self._send(struct.pack(">BxH", 0, 1) + body)


# ── サーバ ───────────────────────────────────────────────────────────────────

# 配る noVNC への手直し (ファイル → (元の文, 直した文))。元の文が見つからなければ直さずに配り、remote.log に書く
NOVNC_PATCHES = {
    # iPad (iOS の Safari) は、日本語の入力中 (変換前) のキーにも、押したキーの文字 ("s" など。keyCode 229) を付けて
    # keydown を出す。noVNC はそれを打ったキーとして送り、さらに入力欄の変化 (「s」→「さ」) からも文字を送るので、
    # 「サンドマン」が「ssさnn…」のように二重になって崩れた。入力中のキーは送らず、入力欄の変化に任せる
    # (Android の Chrome は入力中のキーの文字を "Unidentified" にするので、もともと何も送っていない)
    "core/input/keyboard.js": (
        "        // We cannot handle keys we cannot track, but we also need\n",
        "        // WinTabDock: keys typed while an IME composes (iOS gives them the letter and keyCode 229)\n"
        "        // are sent by the input event of the keyboard field. Do not send them twice.\n"
        "        if (e.isComposing || e.keyCode === 229) {\n"
        "            stopEvent(e);\n"
        "            return;\n"
        "        }\n\n"
        "        // We cannot handle keys we cannot track, but we also need\n",
    ),
    # URL に keylog が付いていたら、キーと入力欄の出来事、PC へ送ったキーを画面の隅に出す (KEYLOG_JS)。
    # iPad が実際に何を出しているかを、iPad の実機が手元に無くても画面写真で見るため
    "vnc.html": ("</head>", '<script type="module" src="wintabdock-keylog.js"></script>\n</head>'),
}

KEYLOG_PATH = "wintabdock-keylog.js"
KEYLOG_JS = r"""// WinTabDock: with ?keylog in the URL, show keyboard events and the keys sent to the PC on screen.
import RFB from './core/rfb.js';

if (new URLSearchParams(location.search).has('keylog')) {
    const box = document.createElement('pre');
    box.id = 'wintabdock_keylog';
    box.style.cssText = 'position:fixed;left:0;bottom:0;z-index:99999;margin:0;padding:4px;max-height:50vh;' +
        'width:70vw;overflow:hidden;background:rgba(0,0,0,.8);color:#7f7;font:11px monospace;' +
        'pointer-events:none;white-space:pre-wrap';
    const lines = [];
    const log = (s) => {
        lines.push(s);
        if (lines.length > 60) lines.shift();
        box.textContent = lines.join('\n');
    };
    const show = (v) => JSON.stringify(v);
    const ready = () => document.body.appendChild(box);
    if (document.readyState === 'loading') addEventListener('DOMContentLoaded', ready); else ready();

    const hook = () => {
        const kbi = document.getElementById('noVNC_keyboardinput');
        if (!kbi) { setTimeout(hook, 200); return; }
        const tail = () => show(kbi.value.replace(/^_+/, '').slice(-8)) + ' sel=' + (kbi.selectionStart - kbi.value.length);
        for (const t of ['keydown', 'keyup']) {
            kbi.addEventListener(t, (e) => log(`${t} key=${show(e.key)} code=${show(e.code)} kc=${e.keyCode} comp=${e.isComposing}`), true);
        }
        for (const t of ['compositionstart', 'compositionupdate', 'compositionend']) {
            kbi.addEventListener(t, (e) => log(`${t} ${show(e.data)}`), true);
        }
        kbi.addEventListener('input', (e) => log(`input ${e.inputType} ${show(e.data)} -> ${tail()}`), true);
    };
    hook();

    // sendKey(keysym, code) は自分で down と up を呼ぶ。1 回の押して離すは 1 行にする
    const orig = RFB.prototype.sendKey;
    let depth = 0;
    RFB.prototype.sendKey = function (keysym, code, down) {
        if (depth === 0) {
            const ch = (keysym >= 0x20 && keysym < 0x100) ? String.fromCharCode(keysym)
                : ((keysym & 0xff000000) === 0x01000000) ? String.fromCodePoint(keysym & 0xffffff)
                : '0x' + keysym.toString(16);
            log(`    send ${ch}${down === undefined ? '' : (down ? ' down' : ' up')}`);
        }
        depth++;
        try {
            return orig.call(this, keysym, code, down);
        } finally {
            depth--;
        }
    };
    log('keylog on');
}
"""


def patch_novnc(rel: str, body: bytes) -> tuple[bytes, bool]:
    """配る noVNC のファイルに手直しを当てる。(中身, 当てられたか)。手直しの無いファイルは当てられた扱い"""
    if rel not in NOVNC_PATCHES:
        return body, True
    old, new = NOVNC_PATCHES[rel]
    text = body.decode("utf-8")
    if old not in text:
        return body, False
    return text.replace(old, new, 1).encode("utf-8"), True


_SAFE_PATH = re.compile(r"[A-Za-z0-9._-]+(/[A-Za-z0-9._-]+)*")  # 配るファイルの道 (noVNC のファイル名の文字だけ)


def new_token() -> str:
    return secrets.token_urlsafe(18)


def novnc_dir(base: Path) -> Path:
    """noVNC を初回にダウンロードして控える (版とハッシュを固定)。"""
    root = base / f"novnc-{NOVNC_VERSION}" / f"noVNC-{NOVNC_VERSION}"
    if (root / "vnc.html").exists():
        return root
    data = urllib.request.urlopen(NOVNC_URL, timeout=60).read()
    digest = hashlib.sha256(data).hexdigest()
    if digest != NOVNC_SHA256:
        raise OSError(f"noVNC のハッシュが合わない: {digest}")
    root.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        tar.extractall(root.parent, filter="data")
    return root


class _SocketAddress(ctypes.Structure):
    _fields_ = [("lpSockaddr", ctypes.c_void_p), ("iSockaddrLength", ctypes.c_int)]


class _UnicastAddress(ctypes.Structure):
    pass


_UnicastAddress._fields_ = [("Length", ctypes.c_ulong), ("Flags", wintypes.DWORD),
                            ("Next", ctypes.POINTER(_UnicastAddress)), ("Address", _SocketAddress)]


class _AdapterAddresses(ctypes.Structure):
    pass


# IP_ADAPTER_ADDRESSES の頭の部分だけ (OperStatus まで)。次へは Next でたどるので、後ろを書かなくてよい
_AdapterAddresses._fields_ = [
    ("Length", ctypes.c_ulong), ("IfIndex", wintypes.DWORD), ("Next", ctypes.POINTER(_AdapterAddresses)),
    ("AdapterName", ctypes.c_char_p), ("FirstUnicastAddress", ctypes.POINTER(_UnicastAddress)),
    ("FirstAnycastAddress", ctypes.c_void_p), ("FirstMulticastAddress", ctypes.c_void_p),
    ("FirstDnsServerAddress", ctypes.c_void_p), ("DnsSuffix", ctypes.c_wchar_p), ("Description", ctypes.c_wchar_p),
    ("FriendlyName", ctypes.c_wchar_p), ("PhysicalAddress", ctypes.c_ubyte * 8), ("PhysicalAddressLength", ctypes.c_ulong),
    ("Flags", ctypes.c_ulong), ("Mtu", ctypes.c_ulong), ("IfType", wintypes.DWORD), ("OperStatus", ctypes.c_int),
]


def local_addresses() -> list[tuple[str, str]]:
    """この PC のネットワークと IPv4 のアドレス [(名前, アドレス)]。名前は Windows の表示名 (Wi-Fi・Tailscale など)。
    つながっているものだけ。この PC の中だけの 127.0.0.1 は含めない。"""
    iphlpapi = ctypes.WinDLL("iphlpapi")
    size = ctypes.c_ulong(16384)
    for _ in range(3):
        buf = ctypes.create_string_buffer(size.value)
        # AF_INET、エニーキャスト・マルチキャスト・DNS のアドレスは要らない
        r = iphlpapi.GetAdaptersAddresses(socket.AF_INET, 0x2 | 0x4 | 0x8, None, buf, ctypes.byref(size))
        if r != 111:  # ERROR_BUFFER_OVERFLOW なら、言われた大きさで取り直す
            break
    if r != 0:
        return []
    out = []
    p = ctypes.cast(buf, ctypes.POINTER(_AdapterAddresses))
    while p:
        a = p.contents
        if a.OperStatus == 1 and a.IfType != 24:  # つながっている、ループバックでない
            u = a.FirstUnicastAddress
            while u:
                sa = u.contents.Address
                if sa.lpSockaddr and sa.iSockaddrLength >= 8:
                    raw = ctypes.string_at(sa.lpSockaddr, 8)
                    out.append((a.FriendlyName or "", socket.inet_ntoa(raw[4:8])))
                u = u.contents.Next
        p = a.Next
    return out


def is_tailscale(ip: str) -> bool:
    a, b = (int(p) for p in ip.split(".")[:2])
    return a == 100 and 64 <= b <= 127


def tailscale_ip() -> str | None:
    """この PC の Tailscale のアドレス (100.64.0.0/10)。無ければ None。"""
    try:
        infos = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
    except OSError:
        return None
    for info in infos:
        a, b = (int(p) for p in info[4][0].split(".")[:2])
        if a == 100 and 64 <= b <= 127:
            return info[4][0]
    return None


class Server:
    """待ち受け。ブラウザに noVNC を配り、合言葉の合う WebSocket だけを VNC の接続にする。"""

    def __init__(self, view: View, host: str, port: int, token: str, web_root: Path,
                 log: Callable[[str], None] = print):
        self.view = view
        self.host, self.port, self.token = host, port, token
        self.web_root = web_root
        self.log = log
        self.sessions: list[Session] = []
        self.lock = threading.Lock()
        self.sock: socket.socket | None = None

    @property
    def url(self) -> str:
        path = urllib.parse.quote(f"websockify?token={self.token}", safe="")
        return f"http://{self.host}:{self.port}/vnc.html?autoconnect=true&resize=remote&reconnect=true&path={path}"

    def peers(self) -> list[str]:
        """いまつないでいる相手のアドレス。"""
        with self.lock:
            return [s.name for s in self.sessions]

    def start(self) -> None:
        self.sock = socket.create_server((self.host, self.port))
        self.sock.settimeout(0.5)
        threading.Thread(target=self._accept, daemon=True).start()

    def stop(self) -> None:
        sock, self.sock = self.sock, None
        if sock:
            sock.close()
        with self.lock:
            for s in self.sessions:
                try:
                    s.s.sock.close()
                except OSError:
                    pass

    def _accept(self) -> None:
        while self.sock:
            try:
                conn, addr = self.sock.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            threading.Thread(target=self._handle, args=(conn, addr), daemon=True).start()

    def _handle(self, sock: socket.socket, addr) -> None:
        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            sock.settimeout(10)
            head = b""
            while b"\r\n\r\n" not in head:
                chunk = sock.recv(4096)
                if not chunk or len(head) > 65536:
                    return
                head += chunk
            sock.settimeout(None)
            lines = head.split(b"\r\n\r\n", 1)[0].decode("latin-1").split("\r\n")
            method, target = (lines[0].split(" ") + ["", ""])[:2]
            path, _, query = target.partition("?")
            headers = {k.strip().lower(): v.strip() for k, _, v in (ln.partition(":") for ln in lines[1:])}
            if method != "GET":
                return
            if headers.get("upgrade", "").lower() == "websocket":
                self._websocket(sock, addr, path, query, headers)
            else:
                self._static(sock, path)
        except (OSError, Closed):
            pass
        finally:
            sock.close()

    def _websocket(self, sock: socket.socket, addr, path: str, query: str, headers: dict) -> None:
        # 合言葉 (URL の token) と Origin を確かめる。Origin を見ないと、ブラウザで開いているだけのよそのサイトの
        # ページがつないでくる。DNS を書き換えて Host ごと揃えてくる手口は、合言葉を知らないので通らない
        token = urllib.parse.parse_qs(query).get("token", [""])[0]
        host = headers.get("host", "").lower()
        ok_token = secrets.compare_digest(token.encode(), self.token.encode())
        if path != "/websockify" or not ok_token or headers.get("origin", "").lower() != f"http://{host}":
            self.log(f"{addr[0]}: 断った ({'合言葉が違う' if not ok_token else 'Origin が違う'})")
            sock.sendall(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            return
        accept = base64.b64encode(hashlib.sha1((headers["sec-websocket-key"] + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11")
                                               .encode()).digest()).decode()
        proto = "Sec-WebSocket-Protocol: binary\r\n" if "binary" in headers.get("sec-websocket-protocol", "") else ""
        sock.sendall(f"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                     f"Sec-WebSocket-Accept: {accept}\r\n{proto}\r\n".encode())
        session = Session(WebSocketStream(sock), self, f"{addr[0]}")
        with self.lock:
            self.sessions.append(session)
            first = len(self.sessions) == 1
        if first:
            self.view.source.connected(True)
        try:
            session.run()
        finally:
            with self.lock:
                self.sessions.remove(session)
                last = not self.sessions
            if last:
                self.view.requested = None
                self.view.source.connected(False)

    def _static(self, sock: socket.socket, path: str) -> None:
        if path == "/":
            sock.sendall(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            return  # 合言葉の入った URL を使ってもらう (トップからは入れない)
        # ファイルに触れる前に道を確かめる。`\` や `C:` が混じると、noVNC のフォルダの外 (\\相手\共有 なら、
        # 相手の PC への接続とサインインの資格情報の送信) を調べに行く。noVNC のファイル名に使う文字だけを通す
        rel = path.lstrip("/")
        ok = bool(_SAFE_PATH.fullmatch(rel)) and ".." not in rel.split("/")
        file = (self.web_root / rel).resolve() if ok else None
        if rel == KEYLOG_PATH:
            body, status, ctype = KEYLOG_JS.encode("utf-8"), "200 OK", "text/javascript"
        elif file and file.is_file() and file.is_relative_to(self.web_root.resolve()):
            body, patched = patch_novnc(rel, file.read_bytes())
            status = "200 OK"
            if not patched:
                self.log(f"noVNC の {rel} に手直しを当てられなかった (元の文が見つからない)")
            elif rel == "core/input/keyboard.js":
                # ページを読み込み直したかを後から見るため (古いページは、つなぎ直しても手直し前のまま動く)
                self.log("noVNC のキー入力 (keyboard.js、手直し済み) を配った")
            ctype = {".js": "text/javascript", ".mjs": "text/javascript"}.get(file.suffix) or \
                mimetypes.guess_type(file.name)[0] or "application/octet-stream"
        else:
            body, status, ctype = b"not found", "404 Not Found", "text/plain"
        head = (f"HTTP/1.1 {status}\r\nContent-Length: {len(body)}\r\nContent-Type: {ctype}\r\n"
                "Cache-Control: no-cache\r\nConnection: close\r\n\r\n")
        sock.sendall(head.encode() + body)
