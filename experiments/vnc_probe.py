# /// script
# requires-python = ">=3.11"
# dependencies = ["pillow", "numpy"]
# ///
"""実験: 1 つの窓を VNC で見せ、つないだ側の画面の大きさに窓を合わせる。

    uv run experiments/vnc_probe.py --title PortWho          # タイトルに PortWho を含む窓
    uv run experiments/vnc_probe.py --exe C:/Tools/x.exe     # 起動して、その窓 (終わるときに閉じる)
    uv run experiments/vnc_probe.py --title X --host 100.x.y.z   # Tailscale のアドレスで待ち受ける

ブラウザで http://127.0.0.1:15900/ を開くと noVNC (ブラウザの VNC クライアント) が出て、
ブラウザの窓の大きさに合わせて相手の窓が変わる。VNC アプリ (Android の AVNC など) は同じポートに直接つなぐ。

確かめたいこと (結果は docs/DEVELOPMENT.md の「遠隔で使う (VNC の試作)」):
  - 隠れた窓・画面の外にはみ出した窓も撮れるか (PrintWindow)
  - つないだ側の大きさ (ExtendedDesktopSize の SetDesktopSize) に窓を合わせられるか。画面より大きい窓は
  - 手元のマウスを動かさずに操作を届けられるか (窓へのメッセージで送る。--input sendinput は本物の入力)

窓の枠には触らない (内側 = クライアント領域だけを撮り、その大きさを合わせる)。終わるときに元の位置・大きさに戻す。
認証は無い。--host は 127.0.0.1 か Tailscale のアドレスにし、インターネットに開けない。
"""

from __future__ import annotations

import argparse
import base64
import ctypes
import hashlib
import io
import mimetypes
import os
import signal
import socket
import struct
import subprocess
import sys
import tarfile
import threading
import time
import urllib.request
from ctypes import wintypes
from pathlib import Path

import numpy as np
from PIL import Image, ImageChops

PORT = 15900  # LPortMan の台帳に「WinTabDock VNC probe」で登録
NOVNC_VERSION = "1.6.0"
NOVNC_URL = f"https://github.com/novnc/noVNC/archive/refs/tags/v{NOVNC_VERSION}.tar.gz"
NOVNC_SHA256 = "5066103959ef4e9b10f37e5a148627360dd8414e4cf8a7db92bdbd022e728aaa"
FRAME_INTERVAL = 1 / 15  # 撮る間隔の下限 (秒)

# ---- Win32 ----

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
user32.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))  # PER_MONITOR_AWARE_V2: 座標は実際の画素

LPARAM = ctypes.c_ssize_t
user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, LPARAM]
user32.SendMessageTimeoutW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, LPARAM, wintypes.UINT,
                                       wintypes.UINT, ctypes.c_void_p]
user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
user32.GetKeyboardState.argtypes = [ctypes.c_void_p]
user32.SetKeyboardState.argtypes = [ctypes.c_void_p]
user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
user32.MapWindowPoints.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.POINTER(wintypes.POINT), wintypes.UINT]
user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT]
user32.ChildWindowFromPointEx.argtypes = [wintypes.HWND, wintypes.POINT, wintypes.UINT]
user32.ChildWindowFromPointEx.restype = wintypes.HWND
user32.PrintWindow.argtypes = [wintypes.HWND, wintypes.HDC, wintypes.UINT]
user32.GetDC.argtypes = [wintypes.HWND]
user32.GetDC.restype = wintypes.HDC
user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
user32.IsWindow.argtypes = [wintypes.HWND]
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
user32.GetWindow.restype = wintypes.HWND
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.VkKeyScanW.argtypes = [wintypes.WCHAR]
user32.VkKeyScanW.restype = ctypes.c_short
user32.MapVirtualKeyW.argtypes = [wintypes.UINT, wintypes.UINT]
user32.GetSystemMetrics.argtypes = [ctypes.c_int]
gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
gdi32.CreateCompatibleDC.restype = wintypes.HDC
gdi32.CreateDIBSection.argtypes = [wintypes.HDC, ctypes.c_void_p, wintypes.UINT, ctypes.POINTER(ctypes.c_void_p), wintypes.HANDLE, wintypes.DWORD]
gdi32.CreateDIBSection.restype = wintypes.HBITMAP
gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
gdi32.SelectObject.restype = wintypes.HGDIOBJ
gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
gdi32.DeleteDC.argtypes = [wintypes.HDC]

EnumWindowsProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, LPARAM)
user32.EnumWindows.argtypes = [EnumWindowsProc, LPARAM]

SWP_NOMOVE, SWP_NOZORDER, SWP_NOACTIVATE = 0x0002, 0x0004, 0x0010
PW_CLIENTONLY, PW_RENDERFULLCONTENT = 0x1, 0x2
CWP_SKIPINVISIBLE, CWP_SKIPTRANSPARENT = 0x1, 0x4
GW_OWNER = 4
SMTO_ABORTIFHUNG = 0x0002
WM_CLOSE = 0x0010
WM_ACTIVATE, WA_ACTIVE, WM_SETFOCUS = 0x0006, 1, 0x0007
WM_KEYDOWN, WM_KEYUP, WM_CHAR = 0x0100, 0x0101, 0x0102
WM_MOUSEMOVE, WM_MOUSEWHEEL = 0x0200, 0x020A
BUTTON_MSGS = {1: (0x0201, 0x0202, 0x0001), 2: (0x0207, 0x0208, 0x0010), 4: (0x0204, 0x0205, 0x0002)}  # 左・中・右: 押す・離す・MK_
INPUT_MOUSE, INPUT_KEYBOARD = 0, 1
MOUSEEVENTF_MOVE, MOUSEEVENTF_ABSOLUTE, MOUSEEVENTF_VIRTUALDESK, MOUSEEVENTF_WHEEL = 0x0001, 0x8000, 0x4000, 0x0800
SENDINPUT_BUTTONS = {1: (0x0002, 0x0004), 2: (0x0020, 0x0040), 4: (0x0008, 0x0010)}
KEYEVENTF_KEYUP, KEYEVENTF_UNICODE = 0x0002, 0x0004


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
                ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG), ("biYPelsPerMeter", wintypes.LONG),
                ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD)]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("pad", ctypes.c_byte * 32)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


class GUITHREADINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("flags", wintypes.DWORD), ("hwndActive", wintypes.HWND),
                ("hwndFocus", wintypes.HWND), ("hwndCapture", wintypes.HWND), ("hwndMenuOwner", wintypes.HWND),
                ("hwndMoveSize", wintypes.HWND), ("hwndCaret", wintypes.HWND), ("rcCaret", wintypes.RECT)]


user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
user32.GetGUIThreadInfo.argtypes = [wintypes.DWORD, ctypes.POINTER(GUITHREADINFO)]


def window_text(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(512)
    user32.GetWindowTextW(hwnd, buf, 512)
    return buf.value


def window_class(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def window_pid(hwnd: int) -> int:
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def window_exe(hwnd: int) -> str:
    """窓を持つプロセスの exe のパス (読めなければ空)。"""
    h = kernel32.OpenProcess(0x1000, False, window_pid(hwnd))  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(1024)
        return buf.value if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)) else ""
    finally:
        kernel32.CloseHandle(h)


def top_windows() -> list[int]:
    """見えていて所有者の無いトップレベルの窓 (ダイアログなどの 2 つ目の窓は除く)。"""
    found: list[int] = []

    def cb(hwnd, _):
        if user32.IsWindowVisible(hwnd) and not user32.GetWindow(hwnd, GW_OWNER) and window_text(hwnd):
            found.append(hwnd)
        return True

    user32.EnumWindows(EnumWindowsProc(cb), 0)
    return found


def lparam_xy(x: int, y: int) -> int:
    return (y & 0xFFFF) << 16 | (x & 0xFFFF)


def client_size(hwnd: int) -> tuple[int, int]:
    r = wintypes.RECT()
    user32.GetClientRect(hwnd, ctypes.byref(r))
    return r.right, r.bottom


# ---- 相手の窓: 撮る・大きさを合わせる・操作を届ける ----

class Target:
    """VNC で見せる 1 つの窓。

    VNC の画面の画素 = 窓の実際の画素 × scale。scale は普段 zoom と同じ。つないだ側が大きさを決めたあと
    (SetDesktopSize)、窓が最小の大きさなどでそれより小さくできなかったときは、scale を下げて縮めて収める
    (PortWho は幅 680、LPortMan は 760 より狭くならず、スマホの縦の幅 412 に届かなかった)。"""

    def __init__(self, hwnd: int, zoom: float, input_mode: str, verbose: bool = False):
        self.hwnd = hwnd
        self.zoom = zoom
        self.input_mode = input_mode
        self.verbose = verbose
        self.lock = threading.Lock()
        r = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(r))
        self.original = (r.left, r.top, r.right - r.left, r.bottom - r.top)
        self.requested: tuple[int, int] | None = None  # つないだ側が決めた VNC の画面の大きさ (無ければ窓に合わせる)
        self.scale = zoom
        self.input_lock = threading.Lock()
        self.buttons = 0
        self.last_xy = (0, 0)
        self.holding = False  # 押している間の移動を送り続けるスレッドが動いているか
        self.press_child = 0  # ボタンを押している間は、押した子窓に送り続ける (ドラッグ)
        self.key_child = 0  # 最後に押した子窓 (フォーカスが分からないときのキーの宛先)

    def alive(self) -> bool:
        return bool(user32.IsWindow(self.hwnd))

    def fb_size(self) -> tuple[int, int]:
        if self.requested:
            return self.requested
        w, h = client_size(self.hwnd)
        return max(1, round(w * self.zoom)), max(1, round(h * self.zoom))

    def _fit_scale(self) -> float:
        """今の窓の大きさを、決められた VNC の画面に収める倍率 (zoom より大きくはしない)。"""
        cw, ch = client_size(self.hwnd)
        if not self.requested or cw <= 0 or ch <= 0:
            return self.zoom
        return min(self.zoom, self.requested[0] / cw, self.requested[1] / ch)

    def _set_client(self, want_w: int, want_h: int) -> None:
        for _ in range(2):  # 枠の幅は DPI やメニューの折り返しで変わるので、測り直してもう一度合わせる
            cw, ch = client_size(self.hwnd)
            r = wintypes.RECT()
            user32.GetWindowRect(self.hwnd, ctypes.byref(r))
            ww, wh = r.right - r.left + want_w - cw, r.bottom - r.top + want_h - ch
            user32.SetWindowPos(self.hwnd, None, 0, 0, ww, wh, SWP_NOMOVE | SWP_NOZORDER | SWP_NOACTIVATE)
            if client_size(self.hwnd) == (want_w, want_h):
                break

    def resize(self, fb_w: int, fb_h: int) -> None:
        """VNC の画面を fb_w x fb_h にし、窓の内側をそれに合わせる。窓が最小の大きさなどで届かなければ、縮めて収める。"""
        with self.lock:
            self.requested = (fb_w, fb_h)
            self._set_client(round(fb_w / self.zoom), round(fb_h / self.zoom))
            s = self._fit_scale()
            if s < self.zoom:
                # 幅が収まらず縮めるなら、その倍率で縦を使い切る高さにする (画面の高さで止まることがある)
                cw, _ = client_size(self.hwnd)
                self._set_client(cw, round(fb_h / s))
            self.scale = self._fit_scale()

    def move_offscreen(self) -> None:
        """窓を画面の右の外に置く。Tk は押した所の画面に別の窓が重なっていると、押されたことにしない
        (WindowFromPoint で見ているらしい)。画面の外なら重なりが無く、PrintWindow で撮れる"""
        vx, vw = user32.GetSystemMetrics(76), user32.GetSystemMetrics(78)
        vy = user32.GetSystemMetrics(77)
        user32.SetWindowPos(self.hwnd, None, vx + vw + 100, vy, 0, 0, 0x0001 | SWP_NOZORDER | SWP_NOACTIVATE)

    def restore(self) -> None:
        if self.alive():
            x, y, w, h = self.original
            user32.SetWindowPos(self.hwnd, None, x, y, w, h, SWP_NOZORDER | SWP_NOACTIVATE)

    def capture(self) -> Image.Image | None:
        """内側を撮る。隠れていても画面の外でも、窓に描かせて撮る (PrintWindow)。"""
        with self.lock:
            w, h = client_size(self.hwnd)
            if w <= 0 or h <= 0:
                return None
            screen_dc = user32.GetDC(None)
            mem_dc = gdi32.CreateCompatibleDC(screen_dc)
            bmi = BITMAPINFOHEADER(ctypes.sizeof(BITMAPINFOHEADER), w, -h, 1, 32, 0, 0, 0, 0, 0, 0)
            bits = ctypes.c_void_p()
            bmp = gdi32.CreateDIBSection(mem_dc, ctypes.byref(bmi), 0, ctypes.byref(bits), None, 0)
            try:
                old = gdi32.SelectObject(mem_dc, bmp)
                ok = user32.PrintWindow(self.hwnd, mem_dc, PW_CLIENTONLY | PW_RENDERFULLCONTENT)
                gdi32.SelectObject(mem_dc, old)
                if not ok:
                    return None
                img = Image.frombuffer("RGB", (w, h), ctypes.string_at(bits, w * h * 4), "raw", "BGRX", 0, 1)
            finally:
                gdi32.DeleteObject(bmp)
                gdi32.DeleteDC(mem_dc)
                user32.ReleaseDC(None, screen_dc)
        self.scale = s = self._fit_scale()  # 手元で窓の大きさを変えられても収まるように、撮るたびに測る
        if s != 1:
            img = img.resize((max(1, round(w * s)), max(1, round(h * s))), Image.Resampling.BILINEAR)
        if img.size != self.fb_size():  # 窓が決められた大きさより小さい。余りは灰色で埋め、左上に寄せる
            canvas = Image.new("RGB", self.fb_size(), (64, 64, 64))
            canvas.paste(img, (0, 0))
            img = canvas
        return img

    # -- マウス --

    def pointer(self, mask: int, fx: int, fy: int) -> None:
        x, y = int(fx / self.scale), int(fy / self.scale)
        with self.input_lock:
            if self.verbose and mask != self.buttons:
                log(f"マウス {self.buttons:#04x} → {mask:#04x} ({x}, {y}) 子窓 {self._child_at(x, y)}")
            if self.input_mode == "sendinput":
                self._pointer_sendinput(mask, x, y)
            else:
                self._pointer_post(mask, x, y)
            self.buttons, self.last_xy = mask, (x, y)
            if self.input_mode == "post" and mask & 7 and not self.holding:
                self.holding = True
                threading.Thread(target=self._hold, daemon=True).start()

    def _hold(self) -> None:
        """ボタンを押している間、同じ位置への移動を送り続ける。Tk は本物のマウスカーソルの位置を 50ms ほどごとに
        見に行き、そこに自分が無いと「出た」ことにして、押した・離したを捨てる (スマホのタップは 100ms ほど押している)"""
        while True:
            time.sleep(0.02)
            with self.input_lock:
                if not self.buttons & 7:
                    self.holding = False
                    return
                self._pointer_post(self.buttons, *self.last_xy)

    def _child_at(self, x: int, y: int) -> tuple[int, int, int]:
        """内側の (x, y) にある一番奥の子窓と、その子窓の中の座標。"""
        hwnd, pt = self.hwnd, wintypes.POINT(x, y)
        while True:
            child = user32.ChildWindowFromPointEx(hwnd, pt, CWP_SKIPINVISIBLE | CWP_SKIPTRANSPARENT)
            if not child or child == hwnd:
                return hwnd, pt.x, pt.y
            user32.MapWindowPoints(hwnd, child, ctypes.byref(pt), 1)
            hwnd = child

    def _pointer_post(self, mask: int, x: int, y: int) -> None:
        if self.buttons & 7 and self.press_child:
            pt = wintypes.POINT(x, y)
            user32.MapWindowPoints(self.hwnd, self.press_child, ctypes.byref(pt), 1)
            child, cx, cy = self.press_child, pt.x, pt.y
        else:
            child, cx, cy = self._child_at(x, y)
        mk = sum(BUTTON_MSGS[b][2] for b in BUTTON_MSGS if mask & b)
        msgs = [(WM_MOUSEMOVE, mk, lparam_xy(cx, cy))]
        for bit, (down, up, _) in BUTTON_MSGS.items():
            if mask & bit and not self.buttons & bit:
                self.press_child = self.key_child = child
                msgs.append((down, mk, lparam_xy(cx, cy)))
            elif self.buttons & bit and not mask & bit:
                msgs.append((up, mk, lparam_xy(cx, cy)))
        if not mask & 7:
            self.press_child = 0
        for bit, delta in ((8, 120), (16, -120)):  # ホイールは押した瞬間だけ。座標は画面の座標
            if mask & bit and not self.buttons & bit:
                pt = wintypes.POINT(x, y)
                user32.ClientToScreen(self.hwnd, ctypes.byref(pt))
                msgs.append((WM_MOUSEWHEEL, (delta & 0xFFFF) << 16 | mk, lparam_xy(pt.x, pt.y)))
        # Tk はボタンを押したかをメッセージではなくマウスの状態 (GetKeyState) で見る。メッセージだけでは押されない。
        # 相手のスレッドと入力の状態を共有して (AttachThreadInput) ボタンの状態を書き換え、相手が処理し終わるまで待つ
        tid = user32.GetWindowThreadProcessId(self.hwnd, None)
        me = kernel32.GetCurrentThreadId()
        attached = user32.AttachThreadInput(me, tid, True)
        if self.verbose and not attached:
            log(f"AttachThreadInput に失敗 (エラー {ctypes.get_last_error()})")
        try:
            if attached:
                state = (ctypes.c_ubyte * 256)()
                user32.GetKeyboardState(state)
                for bit, vk in ((1, 0x01), (2, 0x04), (4, 0x02)):  # VK_LBUTTON / VK_MBUTTON / VK_RBUTTON
                    state[vk] = 0x80 if mask & bit else 0
                user32.SetKeyboardState(state)
            for msg, wp, lp in msgs:
                user32.SendMessageTimeoutW(child, msg, wp, lp, SMTO_ABORTIFHUNG, 500, None)
        finally:
            if attached:
                user32.AttachThreadInput(me, tid, False)
        if any(m in (BUTTON_MSGS[b][0] for b in BUTTON_MSGS) for m, _, _ in msgs):
            # Tk は、自分の窓が前面でない (フォーカスが無い) と思っている間は、届いた文字を捨てる。
            # 押すたびに「有効になった・フォーカスを受けた」と知らせる (本物の前面・フォーカスは動かさない)
            user32.SendMessageTimeoutW(self.hwnd, WM_ACTIVATE, WA_ACTIVE, 0, SMTO_ABORTIFHUNG, 500, None)
            user32.SendMessageTimeoutW(child, WM_SETFOCUS, 0, 0, SMTO_ABORTIFHUNG, 500, None)

    def _pointer_sendinput(self, mask: int, x: int, y: int) -> None:
        pt = wintypes.POINT(x, y)
        user32.ClientToScreen(self.hwnd, ctypes.byref(pt))
        vx, vy = user32.GetSystemMetrics(76), user32.GetSystemMetrics(77)  # 仮想画面の左上・大きさ
        vw, vh = user32.GetSystemMetrics(78), user32.GetSystemMetrics(79)
        ax, ay = (pt.x - vx) * 65535 // max(1, vw - 1), (pt.y - vy) * 65535 // max(1, vh - 1)
        events = [(MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK, 0)]
        for bit, (down, up) in SENDINPUT_BUTTONS.items():
            if mask & bit and not self.buttons & bit:
                events.append((down | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK, 0))
            elif self.buttons & bit and not mask & bit:
                events.append((up | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK, 0))
        for bit, delta in ((8, 120), (16, -120)):
            if mask & bit and not self.buttons & bit:
                events.append((MOUSEEVENTF_WHEEL, delta & 0xFFFFFFFF))
        if mask & 7 and not self.buttons & 7:
            user32.SetForegroundWindow(self.hwnd)
        arr = (INPUT * len(events))()
        for i, (flags, data) in enumerate(events):
            arr[i].type = INPUT_MOUSE
            arr[i].u.mi = MOUSEINPUT(ax, ay, data, flags, 0, 0)
        user32.SendInput(len(events), arr, ctypes.sizeof(INPUT))

    # -- キー --

    def key(self, down: bool, keysym: int) -> None:
        if self.verbose:
            log(f"キー {'押す' if down else '離す'} {keysym:#x}")
        vk = SPECIAL_KEYS.get(keysym)
        ch = None if vk else keysym_char(keysym)
        if vk is None and ch is None:
            return
        if self.input_mode == "sendinput":
            ki = KEYBDINPUT(vk, 0, 0 if down else KEYEVENTF_KEYUP, 0, 0) if vk else \
                KEYBDINPUT(0, ord(ch), KEYEVENTF_UNICODE | (0 if down else KEYEVENTF_KEYUP), 0, 0)
            inp = INPUT(INPUT_KEYBOARD)
            inp.u.ki = ki
            user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))
            return
        dest = self._focus() or self.key_child or self.hwnd
        if self.verbose:
            log(f"  宛先 {dest} {window_class(dest)} (フォーカス {self._focus()}, 最後に押した子窓 {self.key_child})")
        if vk:
            # 修飾キーは窓へのメッセージでは効かない (相手は本物のキーボードの状態を読む)。送らない
            if vk in (0x10, 0x11, 0x12):
                return
            scan = user32.MapVirtualKeyW(vk, 0)
            lp = 1 | scan << 16 | (0 if down else 0xC0000000)
            user32.PostMessageW(dest, WM_KEYDOWN if down else WM_KEYUP, vk, lp)
        elif down:
            units = ch.encode("utf-16-le")
            for unit in struct.unpack(f"<{len(units) // 2}H", units):  # BMP の外の文字は代理対の 2 つに分けて送る
                user32.PostMessageW(dest, WM_CHAR, unit, 1)

    def _focus(self) -> int:
        info = GUITHREADINFO(cbSize=ctypes.sizeof(GUITHREADINFO))
        tid = user32.GetWindowThreadProcessId(self.hwnd, None)
        if user32.GetGUIThreadInfo(tid, ctypes.byref(info)):
            return info.hwndFocus or 0
        return 0


SPECIAL_KEYS = {  # X11 の keysym → 仮想キー
    0xFF08: 0x08, 0xFF09: 0x09, 0xFF0D: 0x0D, 0xFF8D: 0x0D, 0xFF1B: 0x1B, 0xFFFF: 0x2E, 0xFF63: 0x2D,
    0xFF50: 0x24, 0xFF57: 0x23, 0xFF55: 0x21, 0xFF56: 0x22,
    0xFF51: 0x25, 0xFF52: 0x26, 0xFF53: 0x27, 0xFF54: 0x28,
    0xFFE1: 0x10, 0xFFE2: 0x10, 0xFFE3: 0x11, 0xFFE4: 0x11, 0xFFE9: 0x12, 0xFFEA: 0x12,
    **{0xFFBE + i: 0x70 + i for i in range(12)},  # F1-F12
}


def keysym_char(keysym: int) -> str | None:
    if 0x20 <= keysym <= 0x7E or 0xA0 <= keysym <= 0xFF:
        return chr(keysym)
    if keysym & 0xFF000000 == 0x01000000:  # Unicode の keysym (noVNC は日本語の確定文字をこれで送る)
        return chr(keysym & 0xFFFFFF)
    return None


# ---- 通り道: VNC アプリは TCP のまま、ブラウザ (noVNC) は WebSocket ----

class Closed(Exception):
    pass


class TcpStream:
    def __init__(self, sock: socket.socket):
        self.sock = sock

    def recv(self, n: int) -> bytes:
        buf = bytearray()
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise Closed()
            buf += chunk
        return bytes(buf)

    def send(self, data: bytes) -> None:
        self.sock.sendall(data)


class WebSocketStream(TcpStream):
    def __init__(self, sock: socket.socket):
        super().__init__(sock)
        self.buf = bytearray()

    def _raw(self, n: int) -> bytes:
        return TcpStream.recv(self, n)

    def recv(self, n: int) -> bytes:
        while len(self.buf) < n:
            b0, b1 = self._raw(2)
            length = b1 & 0x7F
            if length == 126:
                length = struct.unpack(">H", self._raw(2))[0]
            elif length == 127:
                length = struct.unpack(">Q", self._raw(8))[0]
            mask = self._raw(4) if b1 & 0x80 else b"\0\0\0\0"
            payload = bytes(np.bitwise_xor(np.frombuffer(self._raw(length), np.uint8),
                                           np.resize(np.frombuffer(mask, np.uint8), length))) if length else b""
            opcode = b0 & 0x0F
            if opcode == 8:
                raise Closed()
            if opcode == 9:
                self._frame(0x8A, payload)
            elif opcode in (0, 1, 2):
                self.buf += payload
        data = bytes(self.buf[:n])
        del self.buf[:n]
        return data

    def _frame(self, head: int, data: bytes) -> None:
        n = len(data)
        hdr = bytes([head, n]) if n < 126 else bytes([head, 126]) + struct.pack(">H", n) if n < 65536 \
            else bytes([head, 127]) + struct.pack(">Q", n)
        self.sock.sendall(hdr + data)

    def send(self, data: bytes) -> None:
        self._frame(0x82, data)


# ---- RFB (VNC の手順) ----

ENC_RAW, ENC_TIGHT = 0, 7
PSEUDO_DESKTOP_SIZE, PSEUDO_EXT_DESKTOP_SIZE = -223, -308


class Session:
    """1 つの接続。受ける側のスレッドと、画面を送る側のスレッドの 2 本で動く。"""

    def __init__(self, stream: TcpStream, target: Target, name: str):
        self.s = stream
        self.target = target
        self.name = name
        self.send_lock = threading.Lock()
        self.cond = threading.Condition()
        self.request: tuple[bool, ...] | None = None  # (incremental,) まだ答えていない更新の要求
        self.encodings: set[int] = set()
        self.jpeg_quality = 70
        self.pixfmt = (32, 24, False, True, 255, 255, 255, 16, 8, 0)
        self.fb = target.fb_size()
        self.last: Image.Image | None = None
        self.resize_reply: int | None = None  # SetDesktopSize への返事の状態 (0 = 受け付けた)
        self.announce = False  # 大きさを変えられることを知らせる (ExtendedDesktopSize を一度送る)
        self.closed = False

    def run(self) -> None:
        try:
            self._handshake()
            threading.Thread(target=self._updater, daemon=True).start()
            self._reader()
        except (Closed, ConnectionError, OSError):
            pass
        finally:
            self.closed = True
            with self.cond:
                self.cond.notify_all()
            log(f"{self.name}: 切れた")

    def _send(self, data: bytes) -> None:
        with self.send_lock:
            self.s.send(data)

    def _handshake(self) -> None:
        self.s.send(b"RFB 003.008\n")
        ver = self.s.recv(12)
        if ver[:11] == b"RFB 003.003":
            self.s.send(struct.pack(">I", 1))  # 認証なし
        else:
            self.s.send(b"\x01\x01")  # 認証の種類は 1 つ: なし
            self.s.recv(1)
            self.s.send(struct.pack(">I", 0))
        self.s.recv(1)  # ClientInit (共有するか)
        w, h = self.fb
        name = window_text(self.target.hwnd).encode("utf-8")
        self.s.send(struct.pack(">HH", w, h) + self._pixfmt_bytes() + struct.pack(">I", len(name)) + name)
        log(f"{self.name}: つながった ({ver.strip().decode(errors='replace')}, {w}x{h})")

    def _pixfmt_bytes(self) -> bytes:
        bpp, depth, big, true, rmax, gmax, bmax, rs, gs, bs = self.pixfmt
        return struct.pack(">BBBBHHHBBB3x", bpp, depth, big, true, rmax, gmax, bmax, rs, gs, bs)

    def _reader(self) -> None:
        r = self.s.recv
        while True:
            t = r(1)[0]
            if t == 0:  # SetPixelFormat
                self.pixfmt = struct.unpack(">3xBBBBHHHBBB3x", r(19))
            elif t == 2:  # SetEncodings
                n = struct.unpack(">xH", r(3))[0]
                encs = struct.unpack(f">{n}i", r(4 * n))
                if PSEUDO_EXT_DESKTOP_SIZE in encs and PSEUDO_EXT_DESKTOP_SIZE not in self.encodings:
                    with self.cond:
                        self.announce = True  # noVNC はこれを一度受けてから大きさの要求を送ってくる
                        self.cond.notify_all()
                self.encodings = set(encs)
                for e in encs:
                    if -32 <= e <= -23:
                        self.jpeg_quality = 15 + (e + 32) * 9  # 品質 0..9 → JPEG 15..96
                log(f"{self.name}: 対応する符号 {sorted(self.encodings)}")
            elif t == 3:  # FramebufferUpdateRequest
                inc = r(9)[0]
                with self.cond:
                    self.request = (bool(inc) and (self.request is None or self.request[0]),)
                    self.cond.notify_all()
            elif t == 4:  # KeyEvent
                down, key = struct.unpack(">B2xI", r(7))
                self.target.key(bool(down), key)
            elif t == 5:  # PointerEvent
                mask, x, y = struct.unpack(">BHH", r(5))
                self.target.pointer(mask, x, y)
            elif t == 6:  # ClientCutText (試作では読み捨てる)
                n = struct.unpack(">3xi", r(7))[0]
                r(abs(n))
            elif t == 251:  # SetDesktopSize
                w, h, n = struct.unpack(">xHHBx", r(7))
                r(16 * n)
                if self.target.requested == (w, h) == self.fb:
                    # 今と同じ大きさ。受け付けたと返すと noVNC は表示を作り直し、その拍子に端数のある画面
                    # (devicePixelRatio 1.75 で幅 411.43 など) の大きさの監視が反応して、また同じ大きさを頼んでくる
                    # (実機で 1 秒に 10 回、止まらなかった)。「変えない」(状態 1) と返すと、作り直さずに止まる
                    status = 1
                else:
                    log(f"{self.name}: 大きさの要求 {w}x{h}")
                    self.target.resize(w, h)
                    status = 0
                with self.cond:
                    self.resize_reply = status
                    self.cond.notify_all()
            else:
                raise Closed(f"知らないメッセージ {t}")

    def _updater(self) -> None:
        next_at = 0.0
        while not self.closed:
            with self.cond:
                while not self.closed and self.request is None and self.resize_reply is None and not self.announce:
                    self.cond.wait(0.5)
                if self.closed:
                    return
            time.sleep(max(0.0, next_at - time.monotonic()))
            next_at = time.monotonic() + FRAME_INTERVAL
            if not self.target.alive():
                log("相手の窓が無くなった")
                os._exit(0)
            fb = self.target.fb_size()
            with self.cond:
                reply, self.resize_reply = self.resize_reply, None
                announce, self.announce = self.announce, False
            if fb != self.fb or reply is not None or announce:
                self._send_size(fb, reply)
                if fb != self.fb or reply == 0:
                    self.fb, self.last = fb, None  # 大きさが変わった。次は全体を送る
                continue
            with self.cond:
                req = self.request
            if req is None:
                continue
            img = self.target.capture()
            if img is None or img.size != self.fb:
                continue  # 大きさが変わる途中。次で送り直す
            box = (0, 0, *img.size)
            if req[0] and self.last is not None:
                box = ImageChops.difference(img, self.last).getbbox()
                if box is None:
                    continue  # 変わっていない。要求は残したまま、次を撮る
            with self.cond:
                self.request = None
            self.last = img
            self._send_rect(img.crop(box), box[0], box[1])

    def _send_size(self, fb: tuple[int, int], reply: int | None) -> None:
        w, h = fb
        if PSEUDO_EXT_DESKTOP_SIZE in self.encodings:
            reason, status = (1, reply) if reply is not None else (0, 0)
            screen = struct.pack(">IHHHHI", 0, 0, 0, w, h, 0)
            body = struct.pack(">HHHHi", reason, status, w, h, PSEUDO_EXT_DESKTOP_SIZE) + struct.pack(">B3x", 1) + screen
        elif PSEUDO_DESKTOP_SIZE in self.encodings:
            body = struct.pack(">HHHHi", 0, 0, w, h, PSEUDO_DESKTOP_SIZE)
        else:
            return  # 大きさが変わったことを伝えられない相手。古い大きさのまま送り続ける (はみ出しは切れる)
        self._send(struct.pack(">BxH", 0, 1) + body)
        log(f"{self.name}: 大きさを伝えた {w}x{h}"
            + {None: "", 0: " (要求への返事)", 1: " (同じ大きさなので変えない、と返事)"}[reply])

    def _send_rect(self, img: Image.Image, x: int, y: int) -> None:
        w, h = img.size
        if ENC_TIGHT in self.encodings and self.pixfmt[0] == 32 and self.pixfmt[1] == 24:
            out = io.BytesIO()
            img.save(out, "JPEG", quality=self.jpeg_quality)
            data = out.getvalue()
            n = len(data)
            compact = bytes([n & 0x7F | (0x80 if n > 0x7F else 0)])
            if n > 0x7F:
                compact += bytes([n >> 7 & 0x7F | (0x80 if n > 0x3FFF else 0)])
            if n > 0x3FFF:
                compact += bytes([n >> 14 & 0xFF])
            payload = struct.pack(">HHHHi", x, y, w, h, ENC_TIGHT) + b"\x90" + compact + data  # 0x90 = JPEG
        else:
            payload = struct.pack(">HHHHi", x, y, w, h, ENC_RAW) + self._raw_pixels(img)
        self._send(struct.pack(">BxH", 0, 1) + payload)

    def _raw_pixels(self, img: Image.Image) -> bytes:
        bpp, _depth, big, _true, rmax, gmax, bmax, rs, gs, bs = self.pixfmt
        a = np.asarray(img, dtype=np.uint32)
        px = (a[..., 0] * rmax // 255) << rs | (a[..., 1] * gmax // 255) << gs | (a[..., 2] * bmax // 255) << bs
        dtype = {8: "u1", 16: "u2", 32: "u4"}[bpp]
        return px.astype((">" if big else "<") + dtype).tobytes()


# ---- HTTP (noVNC の配信) ----

def novnc_dir() -> Path:
    """noVNC を初回にダウンロードして控える (版とハッシュを固定)。"""
    base = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "WinTabDock" / f"novnc-{NOVNC_VERSION}"
    root = base / f"noVNC-{NOVNC_VERSION}"
    if (root / "vnc.html").exists():
        return root
    log(f"noVNC {NOVNC_VERSION} をダウンロードする: {NOVNC_URL}")
    data = urllib.request.urlopen(NOVNC_URL, timeout=60).read()
    digest = hashlib.sha256(data).hexdigest()
    if digest != NOVNC_SHA256:
        raise SystemExit(f"noVNC のハッシュが合わない: {digest}")
    base.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        tar.extractall(base, filter="data")
    return root


def serve_http(sock: socket.socket, web_root: Path, target: Target, peer: str, allowed_hosts: set[str]) -> None:
    head = b""
    while b"\r\n\r\n" not in head:
        chunk = sock.recv(4096)
        if not chunk:
            return
        head += chunk
        if len(head) > 65536:
            return
    lines = head.split(b"\r\n\r\n", 1)[0].decode("latin-1").split("\r\n")
    path = lines[0].split(" ")[1].split("?")[0]
    headers = {k.strip().lower(): v.strip() for k, _, v in (ln.partition(":") for ln in lines[1:])}
    if headers.get("upgrade", "").lower() == "websocket":
        # ほかのサイトのページ (ブラウザで開いているだけで) から 127.0.0.1 の WebSocket につながれないように、
        # 自分が配ったページからの接続だけを受ける。Host も見るので、DNS を書き換えて別の名前で来るのも断る
        host = headers.get("host", "").lower()
        if host not in allowed_hosts or headers.get("origin", "").lower() != f"http://{host}":
            log(f"{peer}: 断った (Origin {headers.get('origin')!r}, Host {headers.get('host')!r})")
            sock.sendall(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            return
        accept = base64.b64encode(hashlib.sha1((headers["sec-websocket-key"] + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11")
                                               .encode()).digest()).decode()
        proto = "Sec-WebSocket-Protocol: binary\r\n" if "binary" in headers.get("sec-websocket-protocol", "") else ""
        sock.sendall(f"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                     f"Sec-WebSocket-Accept: {accept}\r\n{proto}\r\n".encode())
        Session(WebSocketStream(sock), target, f"{peer} (ブラウザ)").run()
        return
    if path == "/":
        body, status, ctype = b"", "302 Found\r\nLocation: /vnc.html?autoconnect=true&resize=remote&reconnect=true", ""
    else:
        file = (web_root / path.lstrip("/")).resolve()
        if file.is_file() and file.is_relative_to(web_root.resolve()):
            body, status = file.read_bytes(), "200 OK"
            ctype = {".js": "text/javascript", ".mjs": "text/javascript"}.get(file.suffix) or \
                mimetypes.guess_type(file.name)[0] or "application/octet-stream"
        else:
            body, status, ctype = b"not found", "404 Not Found", "text/plain"
    head = (f"HTTP/1.1 {status}\r\nContent-Length: {len(body)}\r\n"
            + (f"Content-Type: {ctype}\r\n" if ctype else "")
            + "Cache-Control: no-cache\r\nConnection: close\r\n\r\n")
    sock.sendall(head.encode() + body)


def handle(sock: socket.socket, addr, web_root: Path, target: Target, allowed_hosts: set[str]) -> None:
    peer = f"{addr[0]}:{addr[1]}"
    try:
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        sock.settimeout(0.4)
        try:
            first = sock.recv(4, socket.MSG_PEEK)  # VNC アプリは何も送らずにサーバの挨拶を待つ。ブラウザは GET を送る
        except TimeoutError:
            first = b""
        sock.settimeout(None)
        if first.startswith(b"GET"):
            serve_http(sock, web_root, target, peer, allowed_hosts)
        else:
            Session(TcpStream(sock), target, f"{peer} (VNC)").run()
    except (OSError, Closed):
        pass
    finally:
        sock.close()


def log(msg: str) -> None:
    print(time.strftime("%H:%M:%S"), msg, flush=True)


def find_window(args) -> tuple[int, subprocess.Popen | None]:
    if args.hwnd:
        return args.hwnd, None
    proc = None
    if args.exe:
        # 出力はつながない。LPortMan (lportmanw.exe) は stdout に DEVNULL を渡すと、窓を出さずにすぐ終わった
        proc = subprocess.Popen([args.exe])
    exe = os.path.normcase(os.path.abspath(args.exe)) if args.exe else ""
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        for hwnd in top_windows():
            # 起動したプロセスの番号では探さない。PyInstaller の 1 ファイルの exe (PortWho など) は子プロセスが窓を持つ
            if exe and os.path.normcase(window_exe(hwnd)) == exe or not exe and args.title in window_text(hwnd):
                return hwnd, proc
        time.sleep(0.3)
    raise SystemExit("窓が見つからない")


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--title", help="タイトルにこの文字を含む窓")
    g.add_argument("--exe", help="起動して、その窓を使う (終わるときに閉じる)")
    g.add_argument("--hwnd", type=int, help="窓のハンドル")
    ap.add_argument("--host", default="127.0.0.1", help="待ち受けるアドレス (Tailscale のアドレスなど)")
    ap.add_argument("--port", type=int, default=PORT)
    ap.add_argument("--zoom", type=float, default=1.0, help="2 なら、窓を半分の大きさにして 2 倍に拡大して送る (文字が大きく見える)")
    ap.add_argument("--input", choices=["post", "sendinput"], default="post",
                    help="post: 窓へのメッセージ (手元のマウスは動かない) / sendinput: 本物の入力 (窓を前面にする)")
    ap.add_argument("--offscreen", action="store_true",
                    help="窓を画面の外に置く (別の窓が重なっていても押せるように。終わるときに戻す)")
    ap.add_argument("--verbose", action="store_true", help="届いたマウス・キーを書き出す")
    args = ap.parse_args()
    if args.offscreen and args.input == "sendinput":
        ap.error("--offscreen は --input post とだけ使える (本物の入力は画面の上の窓にしか届かない)")

    def stop(*_):
        raise KeyboardInterrupt

    signal.signal(signal.SIGBREAK, stop)  # 試験 (vnc_probe_check.py) が Ctrl+Break で止めても、窓を元に戻してから終わる
    hwnd, proc = find_window(args)
    target = Target(hwnd, args.zoom, args.input, args.verbose)
    if args.offscreen:
        target.move_offscreen()
    web_root = novnc_dir()
    srv = socket.create_server((args.host, args.port))
    allowed_hosts = {f"{h}:{args.port}".lower() for h in {args.host, "127.0.0.1", "localhost"}}
    log(f"窓 {hwnd} 「{window_text(hwnd)}」 を http://{args.host}:{args.port}/ で見せる (Ctrl+C で終わる)")
    if args.host not in ("127.0.0.1", "localhost", "::1"):
        log("注意: 認証は無い。このアドレスに届く人は誰でも操作できる")
    try:
        srv.settimeout(0.5)  # Ctrl+C を受けられるように
        while True:
            try:
                sock, addr = srv.accept()
            except TimeoutError:
                if not target.alive():
                    log("相手の窓が無くなった")
                    break
                continue
            threading.Thread(target=handle, args=(sock, addr, web_root, target, allowed_hosts), daemon=True).start()
    except KeyboardInterrupt:
        pass
    finally:
        target.restore()
        if proc:
            user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)


if __name__ == "__main__":
    main()
