# /// script
# requires-python = ">=3.11"
# dependencies = ["pillow"]
# ///
"""WinTabDock (試作): 別々に起動した exe の窓を 1 つの枠にタブでまとめる。

    uv run wintabdock.py              # wintabdock.toml のツールを並べたドックを開く
    uv run wintabdock.py --rescue     # 落ちたドックが隠したままの窓を元に戻す
    uv run wintabdock.py --selftest LPortMan PortWho   # 自動の確認 (docs/ に画像を書く)

仕組み (仮想タブ): 相手の窓はトップレベルのまま、枠のタイトル・縁を外してドックの本体の上に重ねる。
窓の所有者 (owner) をドックにするので、Windows が常にツールをドックより手前に置き、最小化も一緒になり、
タスクバーのボタンも出ない。選んでいないタブの窓は隠す。
取り込んだ窓の元の状態は state.json に控え、ドックが落ちても次の起動か --rescue で戻す
(ドックが落ちても、所有者が外れるだけで相手の窓とプロセスは残る。experiments/owner_probe.py で確認)。
"""

from __future__ import annotations

__version__ = "0.2.0"  # 版を上げるときは CHANGELOG.md も (packaging/build.ps1 -Zip が zip の名前に使う)

import ctypes
import json
import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
import tomllib
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path

import qrcodegen  # QR コードを作る (Project Nayuki、MIT。1 ファイルを同梱)
import remote
import tlscert  # 遠隔の HTTPS の証明書を作る (標準ライブラリだけ)
import uia  # 本物のマウスしか受けない部品 (エクスプローラーの上の部分) を押す

# ── Win32 ────────────────────────────────────────────────────────────────────

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

GWL_STYLE, GWL_EXSTYLE, GWLP_HWNDPARENT = -16, -20, -8  # トップレベルの窓では HWNDPARENT が所有者
WS_CAPTION, WS_THICKFRAME = 0x00C00000, 0x00040000
WS_MINIMIZEBOX, WS_MAXIMIZEBOX, WS_SYSMENU = 0x00020000, 0x00010000, 0x00080000
WS_EX_TOOLWINDOW, WS_EX_APPWINDOW = 0x00000080, 0x00040000
SW_HIDE, SW_SHOWNOACTIVATE, SW_SHOW, SW_MAXIMIZE, SW_RESTORE = 0, 4, 5, 3, 9
SWP_NOSIZE, SWP_NOMOVE, SWP_NOZORDER, SWP_NOACTIVATE, SWP_FRAMECHANGED = 0x1, 0x2, 0x4, 0x10, 0x20
HWND_TOP = 0
GW_OWNER = 4
GA_ROOT = 2
WM_CLOSE = 0x0010
EVENT_SYSTEM_FOREGROUND = 0x0003
WINEVENT_OUTOFCONTEXT = 0
TH32CS_SNAPPROCESS = 0x2
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

LONG_PTR = ctypes.c_ssize_t
user32.GetWindowLongPtrW.restype = LONG_PTR
user32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
user32.SetWindowLongPtrW.restype = LONG_PTR
user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, LONG_PTR]
user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT]
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.IsWindow.argtypes = [wintypes.HWND]
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.GetWindow.restype = wintypes.HWND
user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
user32.GetAncestor.restype = wintypes.HWND
user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.IsIconic.argtypes = [wintypes.HWND]
user32.IsZoomed.argtypes = [wintypes.HWND]
user32.WindowFromPoint.restype = wintypes.HWND
user32.WindowFromPoint.argtypes = [wintypes.POINT]
user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
user32.SendMessageW.restype = LONG_PTR
user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.GetAsyncKeyState.restype = ctypes.c_short
user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
VK_LBUTTON = 0x01
user32.keybd_event.argtypes = [wintypes.BYTE, wintypes.BYTE, wintypes.DWORD, ctypes.c_size_t]
user32.GetForegroundWindow.restype = wintypes.HWND
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.GetSystemMetrics.argtypes = [ctypes.c_int]

kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                ctypes.POINTER(wintypes.DWORD)]
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.c_void_p]

WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
user32.EnumWindows.argtypes = [WNDENUMPROC, wintypes.LPARAM]
user32.UnhookWinEvent.argtypes = [wintypes.HANDLE]
user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
user32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
WM_QUIT = 0x0012
EVENT_OBJECT_SHOW = 0x8002
OBJID_WINDOW = 0
WINEVENT_SKIPOWNPROCESS = 0x0002
WINEVENTPROC = ctypes.WINFUNCTYPE(None, wintypes.HANDLE, wintypes.DWORD, wintypes.HWND,
                                  wintypes.LONG, wintypes.LONG, wintypes.DWORD, wintypes.DWORD)
user32.SetWinEventHook.restype = wintypes.HANDLE
user32.SetWinEventHook.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.HMODULE, WINEVENTPROC,
                                   wintypes.DWORD, wintypes.DWORD, wintypes.DWORD]


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wintypes.DWORD),
                ("cntThreads", wintypes.DWORD), ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD), ("szExeFile", ctypes.c_wchar * 260)]


def window_pid(hwnd: int) -> int:
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def window_title(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(512)
    user32.GetWindowTextW(hwnd, buf, 512)
    return buf.value


def process_path(pid: int) -> str:
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(1024)
        ok = kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size))
        return buf.value if ok else ""
    finally:
        kernel32.CloseHandle(h)


def _snapshot() -> list[tuple[int, int]]:
    """(pid, 親 pid) の一覧。"""
    snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    out = []
    entry = PROCESSENTRY32W()
    entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
    ok = kernel32.Process32FirstW(snap, ctypes.byref(entry))
    while ok:
        out.append((entry.th32ProcessID, entry.th32ParentProcessID))
        ok = kernel32.Process32NextW(snap, ctypes.byref(entry))
    kernel32.CloseHandle(snap)
    return out


def all_pids() -> list[int]:
    return [pid for pid, _ in _snapshot()]


def process_tree(root_pid: int) -> set[int]:
    """root_pid とその子孫。PyInstaller の onefile は子プロセスが窓を出すため。"""
    children: dict[int, list[int]] = {}
    for pid, parent in _snapshot():
        children.setdefault(parent, []).append(pid)
    out, stack = set(), [root_pid]
    while stack:
        p = stack.pop()
        if p not in out:
            out.add(p)
            stack.extend(children.get(p, []))
    return out


def top_windows() -> list[int]:
    """見えていて、所有者がなく、タイトルバーのあるトップレベルの窓。"""
    found: list[int] = []

    def cb(hwnd, _):
        if (user32.IsWindowVisible(hwnd) and not user32.GetWindow(hwnd, GW_OWNER)
                and user32.GetWindowLongPtrW(hwnd, GWL_STYLE) & WS_CAPTION == WS_CAPTION and window_title(hwnd)):
            found.append(hwnd)
        return True

    user32.EnumWindows(WNDENUMPROC(cb), 0)
    return found


def rect_of(hwnd: int) -> tuple[int, int, int, int]:
    r = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    return r.left, r.top, r.right - r.left, r.bottom - r.top


def find_window(pids: set[int], exe: str | None = None) -> int | None:
    best, area = None, -1
    for hwnd in top_windows():
        pid = window_pid(hwnd)
        if pid in pids or (exe and os.path.normcase(process_path(pid)) == os.path.normcase(exe)):
            _, _, w, h = rect_of(hwnd)
            if w * h > area:
                best, area = hwnd, w * h
    return best


# ── 取り込んだ窓の控え (ドックが落ちても戻せるように) ─────────────────────────

# 控えはドック (プロセス) ごとに分ける。1 つのファイルを共有していたとき、試験で開いた 2 つ目のドックが
# 動いている方のドックの控えを「前回落ちたもの」と取り違え、その窓を元に戻したうえで控えを消した
# 控えと前回のタブの置き場所。WINTABDOCK_HOME で変えられる (exe を試すとき、人の前回のタブに触れないように)
STATE_DIR = Path(os.environ.get("WINTABDOCK_HOME") or Path(os.environ.get("LOCALAPPDATA", ".")) / "WinTabDock")

# exe (PyInstaller) では、設定 (wintabdock.toml) は exe の隣、同梱したアイコンは _MEIPASS にある
FROZEN = bool(getattr(sys, "frozen", False))
APP_DIR = Path(sys.executable).resolve().parent if FROZEN else Path(__file__).resolve().parent
RES_DIR = Path(getattr(sys, "_MEIPASS", APP_DIR))
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def autostart_enabled() -> bool:
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            return winreg.QueryValueEx(k, "WinTabDock")[0] == f'"{sys.executable}"'
    except OSError:
        return False


def set_autostart(on: bool) -> None:
    """ログイン時に起動する (HKCU の Run)。登録するのはいま動いている exe の場所なので、移動したら入れ直す。"""
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if on:
            winreg.SetValueEx(k, "WinTabDock", 0, winreg.REG_SZ, f'"{sys.executable}"')
        else:
            try:
                winreg.DeleteValue(k, "WinTabDock")
            except FileNotFoundError:
                pass
LEGACY_STATE = STATE_DIR / "state.json"  # 以前の共有の控え。--rescue でだけ扱う


def state_path(pid: int | None = None) -> Path:
    return STATE_DIR / f"state-{pid or os.getpid()}.json"


STATE = state_path()  # このドックの控え


def save_state(tabs: list["Tab"]) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps([{"hwnd": t.hwnd, "style": t.style, "exstyle": t.exstyle, "rect": t.rect,
                                  "owner": t.owner, "maximized": t.maximized} for t in tabs]), encoding="utf-8")


def other_docks() -> list[int]:
    """このプロセス以外で開いているドックの窓。"""
    return [h for h in top_windows() if window_title(h) == "WinTabDock" and window_pid(h) != os.getpid()]


def restore_window(hwnd: int, style: int, exstyle: int, rect: tuple[int, int, int, int], owner: int = 0,
                   maximized: bool = False) -> None:
    if not user32.IsWindow(hwnd):
        return
    user32.ShowWindow(hwnd, SW_HIDE)  # 拡張スタイルの変更をタスクバーに反映させるため、一度隠す
    user32.SetWindowLongPtrW(hwnd, GWLP_HWNDPARENT, owner)
    user32.SetWindowLongPtrW(hwnd, GWL_STYLE, style)
    user32.SetWindowLongPtrW(hwnd, GWL_EXSTYLE, exstyle)
    x, y, w, h = rect
    user32.SetWindowPos(hwnd, None, x, y, w, h, SWP_NOZORDER | SWP_NOACTIVATE | SWP_FRAMECHANGED)
    user32.ShowWindow(hwnd, SW_MAXIMIZE if maximized else SW_SHOW)
    # 属性を元に戻して見せても、タスクバーがボタンを付け直さないことがある (自動で取り込んだ PortWho で、
    # ドックを閉じたあとタスクバーに出なかった)。所有者なし・WS_EX_TOOLWINDOW なしの窓だけ、明示的に足す
    if not owner and not exstyle & WS_EX_TOOLWINDOW:
        taskbar_add(hwnd)


class GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_ulong), ("Data2", ctypes.c_ushort), ("Data3", ctypes.c_ushort),
                ("Data4", ctypes.c_ubyte * 8)]


def _guid(s: str) -> GUID:
    g = GUID()
    ctypes.oledll.ole32.CLSIDFromString(ctypes.c_wchar_p(s), ctypes.byref(g))
    return g


def taskbar_add(hwnd: int) -> bool:
    """ITaskbarList::AddTab でタスクバーにボタンを足す。失敗しても落とさない (見た目だけの問題なので)。"""
    ole32 = ctypes.windll.ole32
    ole32.CoInitialize(None)  # すでに初期化済みなら S_FALSE / RPC_E_CHANGED_MODE。どちらでも使える
    ptr = ctypes.c_void_p()
    hr = ole32.CoCreateInstance(ctypes.byref(_guid("{56FDF344-FD6D-11d0-958A-006097C9A090}")), None, 1,  # CLSID_TaskbarList
                                ctypes.byref(_guid("{56FDF342-FD6D-11d0-958A-006097C9A090}")), ctypes.byref(ptr))  # IID_ITaskbarList
    if hr != 0 or not ptr:
        return False
    vtbl = ctypes.cast(ptr, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
    proto = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p)
    proto_hwnd = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, wintypes.HWND)
    try:
        # vtable: 0 QueryInterface / 1 AddRef / 2 Release / 3 HrInit / 4 AddTab / 5 DeleteTab / ...
        return proto(vtbl[3])(ptr) == 0 and proto_hwnd(vtbl[4])(ptr, hwnd) == 0
    finally:
        proto(vtbl[2])(ptr)


def _restore_file(path: Path) -> int:
    n = 0
    for s in json.loads(path.read_text(encoding="utf-8")):
        if user32.IsWindow(s["hwnd"]):
            restore_window(s["hwnd"], s["style"], s["exstyle"], tuple(s["rect"]), s.get("owner", 0),
                           s.get("maximized", False))
            n += 1
    path.unlink()
    return n


def rescue(include_legacy: bool = False) -> int:
    """もう動いていないドックの控えから窓を戻す。動いているドックの控えには触らない。"""
    if not STATE_DIR.exists():
        return 0
    alive = set(all_pids())
    n = 0
    for path in STATE_DIR.glob("state-*.json"):
        pid = int(path.stem.split("-", 1)[1]) if path.stem.split("-", 1)[1].isdigit() else 0
        if pid and pid not in alive:
            n += _restore_file(path)
    # 以前の共有の控えは、どのドックのものか分からない。ほかにドックが開いていないときに、明示されたらだけ戻す
    if include_legacy and LEGACY_STATE.exists() and not other_docks():
        n += _restore_file(LEGACY_STATE)
    return n


# ── ドック ───────────────────────────────────────────────────────────────────


@dataclass
class Tab:
    name: str
    hwnd: int
    style: int
    exstyle: int
    rect: tuple[int, int, int, int]
    button: tk.Button | None = None
    exe: str = ""  # normcase 済み。同じツールの二重起動を避けるため
    owner: int = 0  # 取り込む前の所有者 (ふつうは 0)
    pid: int = 0  # 窓のプロセス。自動取り込みで、同じプロセスの 2 つ目の窓 (設定画面など) をタブにしないため
    maximized: bool = False  # 取り込む前に最大化していた (外すときに最大化に戻す)
    button_hwnd: int = 0  # タブのボタンの窓。遠隔から押してよい所か見分けるため (別のスレッドから Tk に触らない)


class RemoteSource:
    """ドックを遠隔 (remote.py) に見せる。別のスレッドから呼ばれるので、Tk には触らず Win32 だけで調べる。
    見せるのはドックの内側と、選んでいるツール (と、そのツールが出したダイアログ)。"""

    def __init__(self, dock: "Dock"):
        self.dock = dock

    def _active(self) -> int:
        t = self.dock.active
        return t.hwnd if t and user32.IsWindow(t.hwnd) and user32.IsWindowVisible(t.hwnd) else 0

    def layers(self) -> list:
        d = self.dock.dock_hwnd
        ox, oy = remote.client_origin(d)
        w, h = remote.client_size(d)
        out = [(d, 0, 0, w, h)]
        a = self._active()
        popups = tool_popups(a, {t.hwnd for t in self.dock.tabs} | {d}) if a else []
        # 写していないツール (別のタブ・「+ 開く」の一覧で隠したツール) の小窓は、ドックの上に移していたら元の位置に戻す。
        # 写さないのにドックに重なっていて、その下のボタンが押せなかった (Tk は押した所に別の窓があると押されない)。
        # 選ばれたら下でまた移す
        for hwnd in [h for h in list(self.dock.remote_moved) if h not in popups]:
            if user32.IsWindow(hwnd) and user32.IsWindowVisible(hwnd):
                x, y = self.dock.remote_moved.pop(hwnd)
                user32.SetWindowPos(hwnd, None, x, y, 0, 0, SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE)
        if a:
            if self.dock._remote_geometry:  # つないでいる間 (ドックが画面の外にある間) だけ
                for hwnd in popups:
                    self._bring_in(hwnd, ox, oy, w, h)
            for hwnd in [a] + popups:
                x, y = remote.client_origin(hwnd)
                out.append((hwnd, x - ox, y - oy, *remote.client_size(hwnd)))
        return out

    def _bring_in(self, hwnd: int, ox: int, oy: int, w: int, h: int) -> None:
        """ツールの小窓 (検索など) を、見せる範囲 (ドックの上) に入れる。ドックは画面の外にあるので、ツールが小窓を
        画面の中に収めようとして PC の画面に出した (Mery の検索は画面の右端へ寄せられた)。横は入らなければ真ん中、
        縦は入らなければ下を揃える。元の位置は覚えておき、切れたら戻す (戻さないと、次に PC で開いた時も画面の外に出る)"""
        left, top, pw, ph = rect_of(hwnd)
        x, y = left - ox, top - oy
        nx = x if 0 <= x <= w - pw else max(0, (w - pw) // 2)
        ny = y if 0 <= y <= h - ph else max(0, min(y, h - ph))
        if (nx, ny) != (x, y):
            self.dock.remote_moved.setdefault(hwnd, (left, top))
            user32.SetWindowPos(hwnd, None, ox + nx, oy + ny, 0, 0, SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE)

    def moved_by(self, hwnd: int) -> tuple[int, int]:
        """_bring_in で小窓を動かした量 (動かしていなければ 0, 0)。"""
        orig = self.dock.remote_moved.get(hwnd)
        if not orig:
            return 0, 0
        left, top, _, _ = rect_of(hwnd)
        return left - orig[0], top - orig[1]

    def extent(self) -> tuple[int, int]:
        layers = self.layers()[:2]  # ドックと選んでいるツール (ダイアログははみ出したら切る)
        return max(x + w for _, x, _, w, _ in layers), max(y + h for _, _, y, _, h in layers)

    def set_client_size(self, w: int, h: int) -> None:
        self.dock.call_in_tk(lambda: self.dock.remote_resize(w, h))

    def key_window(self) -> int:
        return self._active()

    def click_allowed(self, top: int, child: int, mask: int) -> bool:
        if top != self.dock.dock_hwnd:
            return True
        # ドックで押せるのはタブと帯のボタン (「+ 開く」と表示倍率) と、「+ 開く」の一覧のボタン (左ボタン) だけ。
        # 「+ 起動」「設定」やタブの右クリックはメニューを出し、メニューは別の窓なので遠隔からは見えず、開いている間ドックが止まる
        allowed = {t.button_hwnd for t in self.dock.tabs} | self.dock.zoom_hwnds | self.dock.launch_hwnds
        return mask & 7 == 1 and child in allowed

    def connected(self, on: bool) -> None:
        self.dock.call_in_tk(lambda: self.dock.remote_connected(on), wait=False)

    def title(self) -> str:
        return "WinTabDock"


# ツールの小窓として見せない窓の種類 (メニューは remote.Menus が扱う。IME・ヒントは見せるものではない)
NOT_POPUPS = {"#32768", "IME", "MSCTFIME UI", "tooltips_class32", "THintWindow", "SysShadow"}


def is_app_window(hwnd: int) -> bool:
    """タスクバーに出る普通のアプリの窓 (持ち主が無く、タイトルバーがあり、WS_EX_TOOLWINDOW でない)。"""
    ex = user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
    return (not user32.GetWindow(hwnd, GW_OWNER) and user32.GetWindowLongPtrW(hwnd, GWL_STYLE) & WS_CAPTION == WS_CAPTION
            and bool(ex & WS_EX_APPWINDOW or not ex & WS_EX_TOOLWINDOW))


def tool_popups(hwnd: int, exclude: set[int]) -> list[int]:
    """ツール hwnd が出した、見えている窓 (ダイアログなど)。hwnd が持ち主の窓と、同じスレッドのほかの窓
    (持ち主の無い小窓もある)。exclude (タブにしている窓・ドック) と、大きさの無い窓 (Delphi の TApplication) は除く。
    同じスレッドでも、普通のアプリの窓は除く。VS Code はどの窓も 1 つのプロセスなので、ほかの VS Code の窓まで
    小窓としてドックの上へ移し、スマホの画面では帯ごと覆った (ユーザーの報告、2026-10-28)。
    プロセスでなくスレッドで見る。エクスプローラーの窓はタスクバー・デスクトップと同じ explorer.exe なので、
    プロセスで見るとタスクバーとデスクトップ (Shell_TrayWnd・Progman) まで小窓としてドックの上へ移した (2026-10-30)"""
    tid = user32.GetWindowThreadProcessId(hwnd, None)
    found: list[int] = []
    buf = ctypes.create_unicode_buffer(64)

    def cb(h, _):
        if h == hwnd or h in exclude or not user32.IsWindowVisible(h):
            return True
        if user32.GetWindow(h, GW_OWNER) == hwnd or (user32.GetWindowThreadProcessId(h, None) == tid
                                                     and not is_app_window(h)):
            user32.GetClassNameW(h, buf, 64)
            _, _, w, hh = rect_of(h)
            if buf.value not in NOT_POPUPS and w > 0 and hh > 0 \
                    and not (buf.value in uia.BRIDGE_CLASSES and uia.is_tooltip(h)):
                found.append(h)
        return True

    user32.EnumWindows(WNDENUMPROC(cb), 0)
    return found[::-1]  # EnumWindows は手前から。重ねるのは奥から


def qr_photo(text: str, size: int = 240) -> tk.PhotoImage:
    """text の QR コードの画像 (一辺が size 以下)。周りに 4 マス分の白い余白を付ける (カメラが読むのに要る)。
    画面に映して読むので汚れや欠けは無い。誤り訂正は一番低くして、マスを粗く (大きく) する"""
    qr = qrcodegen.QrCode.encode_text(text, qrcodegen.QrCode.Ecc.LOW)
    n = qr.get_size() + 8
    img = tk.PhotoImage(width=n, height=n)
    img.put(" ".join("{" + " ".join("#000000" if qr.get_module(x, y) else "#ffffff" for x in range(-4, n - 4)) + "}"
                     for y in range(-4, n - 4)))
    return img.zoom(max(1, size // n))


TAB_BG, TAB_ACTIVE, TAB_FG, TAB_FG_DIM, BAR_BG ="#252542", "#7c3aed", "#ffffff", "#9a9ab8", "#1a1a2e"
BAR_EDGE = "#7c3aed"  # 帯の下の区切り線と「設定」の枠 (アイコンの縁取りと同系の紫)


class Dock:
    def __init__(self, tools: list[dict], settings: dict | None = None, session_path: Path | None = None,
                 prefs_path: Path | None = None):
        settings = settings or {}
        self.tools = tools
        self.auto_adopt = settings.get("auto_adopt", True)
        self.restore_session = settings.get("restore_session", True)
        self.session_path = session_path or STATE_DIR / "session.json"
        # メニューで変える設定 (起動時に開くもの)。wintabdock.toml は人が書くものなので、プログラムからは書き換えない
        self.prefs_path = prefs_path or STATE_DIR / "settings.json"
        self.prefs = self.load_prefs()
        self.ignored: set[int] = set()  # 「ドックから外す」で外した窓。すぐ自動で取り込み直さない
        self.remote_settings = settings.get("remote", {})  # wintabdock.toml の [remote] (host, port)
        self.remote_server: remote.Server | None = None
        self._remote_starting = False  # 待ち受けの準備中 (noVNC の取得など。別のスレッド)
        self._remote_how = ""  # 待ち受けるアドレスの決まり方 (画面に出す)
        self._remote_panel: tk.Toplevel | None = None
        self._remote_notice: tk.Toplevel | None = None  # つないでいる間、PC の画面に出す知らせ
        self._remote_geometry: str | None = None  # 遠隔からつないでいる間 (ドックを画面の外に置く間) の、元の大きさ・位置
        self.remote_moved: dict[int, tuple[int, int]] = {}  # つないでいる間にドックの上へ移したツールの小窓 → 元の位置
        self.launch_panel: tk.Frame | None = None  # スマホの「+ 開く」で、ドックの中に出すツールの一覧
        self.launch_hwnds: set[int] = set()  # その一覧のボタン (遠隔から押してよい)
        self._tk_calls: queue.SimpleQueue = queue.SimpleQueue()  # 別のスレッドから Tk で動かしてほしいこと
        self.dock_hwnd = 0
        self._closing = False
        self.tabs: list[Tab] = []
        self.active: Tab | None = None
        self.root = tk.Tk()
        self.root.title("WinTabDock")
        try:
            self.root.iconbitmap(default=str(RES_DIR / "assets" / "icon.ico"))  # 窓とタスクバーのアイコン
        except tk.TclError:
            pass
        session = self.load_session()
        self.root.geometry(session.get("geometry") or "1100x720+80+60")
        self.root.configure(bg=BAR_BG)
        self.bar = tk.Frame(self.root, bg=BAR_BG, height=36)
        self.bar.pack(side=tk.TOP, fill=tk.X)
        self.tabs_frame = tk.Frame(self.bar, bg=BAR_BG)
        self.tabs_frame.pack(side=tk.LEFT, fill=tk.Y)
        # 右端から: 設定 / + 起動 / 取り込む (side=RIGHT は先に置いたものほど右に並ぶ)。
        # 「設定」は枠線付きにして、取り込んだツールの中の「設定」(RepoTether のタブなど) と見分けられるようにする
        # 枠は 1px 大きい紫の Frame で包んで描く (Windows の tk.Button は highlightthickness の枠を描かなかった)
        self.settings_frame = tk.Frame(self.bar, bg=BAR_EDGE, padx=1, pady=1)
        self.settings_button = tk.Button(self.settings_frame, text="設定 ▾", command=self.show_settings, bg=BAR_BG,
                                         fg=TAB_FG, relief=tk.FLAT, activebackground=TAB_ACTIVE, padx=10, bd=0)
        self.settings_button.pack()
        self.menu_button = tk.Button(self.bar, text="+ 起動 / 取り込む", command=self.show_menu, bg=TAB_BG, fg=TAB_FG,
                                     relief=tk.FLAT, activebackground=TAB_ACTIVE, padx=10)
        self.pack_bar_buttons()
        # スマホとつないでいる間だけ「+ 起動」「設定」の代わりに出す、表示倍率のボタン (remote_connected で入れ替える)
        self.zoom_frame = tk.Frame(self.bar, bg=BAR_BG)
        zoom_button = {"bg": TAB_BG, "fg": TAB_FG, "relief": tk.FLAT, "activebackground": TAB_ACTIVE,
                       "disabledforeground": "#55557a", "padx": 8, "cursor": "hand2"}
        # 「+ 起動」はメニュー (別の窓) を出すので遠隔からは使えない。代わりにドックの中に一覧を出す。
        # 選んでいるタブ (紫の塗り) と並ぶので、塗りでなく「設定」と同じ紫の枠で見分ける (塗りにしたら、タブとつながって見えた)。
        # 帯の左端に置く (右の倍率のボタンと並べていたら、窓が多いときに隠れた。ユーザーの報告、2026-10-30)
        self.open_frame = tk.Frame(self.bar, bg=BAR_EDGE, padx=1, pady=1)
        self.open_button = tk.Button(self.open_frame, text="+ 開く", command=self.toggle_launch_panel, bd=0,
                                     **{**zoom_button, "bg": BAR_BG})
        self.open_button.pack()
        self.zoom_out_button = tk.Button(self.zoom_frame, text="− 縮小", command=lambda: self.change_zoom(-1), **zoom_button)
        self.zoom_out_button.pack(side=tk.LEFT, padx=(8, 0), pady=4)
        self.zoom_label = tk.Label(self.zoom_frame, bg=BAR_BG, fg=TAB_FG_DIM, width=5)  # 今の倍率 (押せない表示)
        self.zoom_label.pack(side=tk.LEFT)
        self.zoom_in_button = tk.Button(self.zoom_frame, text="+ 拡大", command=lambda: self.change_zoom(1), **zoom_button)
        self.zoom_in_button.pack(side=tk.LEFT, padx=(0, 8), pady=4)
        self.zoom_hwnds = {self.open_button.winfo_id(), self.zoom_out_button.winfo_id(), self.zoom_in_button.winfo_id()}
        self.show_zoom()
        # タブの帯と、取り込んだツールの窓の境目。ツールの上端にも帯と似た見た目 (タブや「設定」) があると、
        # どこまでがドックか分かりにくかった
        tk.Frame(self.root, bg=BAR_EDGE, height=2).pack(side=tk.TOP, fill=tk.X)
        self.body = tk.Frame(self.root, bg="#0d0d1a")
        self.body.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        self.empty = tk.Label(self.body, text="右上の「+ 起動 / 取り込む」からツールを開きます",
                              bg="#0d0d1a", fg=TAB_FG_DIM)
        self.empty.place(relx=0.5, rely=0.5, anchor=tk.CENTER)
        self.root.protocol("WM_DELETE_WINDOW", self.request_close)
        self.root.bind("<Configure>", lambda e: self.after_layout())
        self.root.bind("<Unmap>", lambda e: self.on_minimize() if e.widget is self.root else None)
        self.root.bind("<Map>", lambda e: self.on_restore() if e.widget is self.root else None)
        self._layout_pending = False
        self.root.after(500, self.poll)
        # 窓が現れたことはフックで受ける (自動取り込みと、遠隔でつないでいる間に開いた窓をタブにするため)。
        # フックは別スレッドで受け、キューで渡す (tkinter の mainloop と同じスレッドで ctypes のコールバックを
        # 受けると Python ごと落ちた)
        self._shown: queue.SimpleQueue[int] = queue.SimpleQueue()
        self._hook_thread_id = 0
        threading.Thread(target=self._watch_windows, daemon=True).start()
        self.root.after(150, self._drain_shown)
        self.root.after(300, lambda: self.startup(session))
        self.root.after(20, self._drain_tk_calls)
        if self.prefs.get("remote"):
            self.root.after(600, self.start_remote)

    # ── 自動取り込み / 前回のタブ ──
    def _watch_windows(self) -> None:
        self._hook_thread_id = kernel32.GetCurrentThreadId()

        def on_show(_hook, _event, hwnd, id_object, id_child, _thread, _time):
            if hwnd and id_object == OBJID_WINDOW and id_child == 0:
                self._shown.put(hwnd)

        proc = WINEVENTPROC(on_show)
        hook = user32.SetWinEventHook(EVENT_OBJECT_SHOW, EVENT_OBJECT_SHOW, None, proc, 0, 0,
                                      WINEVENT_OUTOFCONTEXT | WINEVENT_SKIPOWNPROCESS)
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        user32.UnhookWinEvent(hook)

    def _drain_shown(self) -> None:
        seen = set()
        while not self._shown.empty():
            seen.add(self._shown.get())
        for hwnd in seen:
            # 現れた直後は、タイトルや大きさがまだ決まっていないことがあるので少し待つ
            self.root.after(400, lambda h=hwnd: self.on_window_shown(h))
        self.root.after(150, self._drain_shown)

    def on_window_shown(self, hwnd: int) -> None:
        if self.auto_adopt:
            self.maybe_adopt(hwnd)
        if self._remote_geometry:
            self.maybe_adopt_remote(hwnd)

    def maybe_adopt_remote(self, hwnd: int) -> None:
        """遠隔でつないでいる間に開いた窓を、どのアプリのものでもタブにして選ぶ (スマホに出す)。ツールが外のアプリ
        (RepoTether が開く VS Code・ターミナル・エクスプローラーなど) を開くと、PC の画面に出てスマホからは見えなかった。
        すでにタブにしているアプリの 2 つ目の窓は、タブにせず小窓として重ねる (RemoteSource)。ただし普通のアプリの窓
        (VS Code の 2 つ目の窓など) は別のタブにする。切ってもタブのまま残す"""
        if self._closing or hwnd in self.ignored or hwnd == self.hwnd or any(t.hwnd == hwnd for t in self.tabs):
            return
        if (not user32.IsWindow(hwnd) or not user32.IsWindowVisible(hwnd) or user32.GetWindow(hwnd, GW_OWNER)
                or user32.GetWindowLongPtrW(hwnd, GWL_STYLE) & WS_CAPTION != WS_CAPTION or not window_title(hwnd)):
            return
        pid = window_pid(hwnd)
        if any(t.pid == pid for t in self.tabs) and not is_app_window(hwnd):
            return  # 小窓として重ねる (tool_popups)。VS Code の 2 つ目の窓のような普通のアプリの窓は、別のタブにする
        exe = process_path(pid)
        tool = self.tool_of_exe(exe)
        self.remote_log(f"つないでいる間に開いた窓をタブにした: {window_title(hwnd)} ({Path(exe).name})")
        self.adopt(tool["name"] if tool else Path(exe).stem or "窓", hwnd)

    def tool_of_exe(self, exe: str) -> dict | None:
        return next((t for t in self.tools if os.path.normcase(t["exe"]) == os.path.normcase(exe)), None)

    def maybe_adopt(self, hwnd: int) -> None:
        """登録済みのツールの最初の窓なら取り込む。外した窓・2 つ目の窓・ダイアログは取り込まない。"""
        if self._closing or hwnd in self.ignored or hwnd == self.hwnd or any(t.hwnd == hwnd for t in self.tabs):
            return
        if (not user32.IsWindow(hwnd) or not user32.IsWindowVisible(hwnd) or user32.GetWindow(hwnd, GW_OWNER)
                or user32.GetWindowLongPtrW(hwnd, GWL_STYLE) & WS_CAPTION != WS_CAPTION or not window_title(hwnd)):
            return
        pid = window_pid(hwnd)
        if any(t.pid == pid for t in self.tabs):
            return  # 同じプロセスの 2 つ目の窓 (設定画面など)
        tool = self.tool_of_exe(process_path(pid))
        if tool:
            self.adopt(tool["name"], hwnd)

    STARTUP_MODES = {"session": "前回開いていたもの", "selected": "選んだものだけ", "none": "何も開かない"}

    @property
    def startup_mode(self) -> str:
        # メニューで選んだもの。まだ選んでいなければ wintabdock.toml の restore_session に従う
        mode = self.prefs.get("startup")
        return mode if mode in self.STARTUP_MODES else ("session" if self.restore_session else "none")

    def startup_names(self, session: dict) -> list[str]:
        """起動時に開くツールの名前 (この並びでタブにする)。"""
        if self.startup_mode == "session":
            return session.get("tabs", [])
        if self.startup_mode == "selected":
            chosen = set(self.prefs.get("startup_tools", []))
            return [t["name"] for t in self.tools if t["name"] in chosen]  # wintabdock.toml の並び
        return []

    def startup(self, session: dict) -> None:
        """起動時に開くものを開き (起動中なら取り込み、なければ起動)、ほかに起動中の登録済みツールも取り込む。"""
        for name in self.startup_names(session):
            tool = next((t for t in self.tools if t["name"] == name), None)
            if tool:
                self.launch(tool)
        if self.auto_adopt:
            for hwnd in top_windows():
                self.maybe_adopt(hwnd)
        want = session.get("active") if self.startup_mode == "session" else None
        active = next((t for t in self.tabs if t.name == want), None)
        if active:
            self.select(active)

    def load_session(self) -> dict:
        """前回のタブと大きさ・位置。大きさ・位置は起動時に開くものの選び方によらず戻す。"""
        try:
            return json.loads(self.session_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def load_prefs(self) -> dict:
        try:
            return json.loads(self.prefs_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def save_prefs(self) -> None:
        self.prefs_path.parent.mkdir(parents=True, exist_ok=True)
        self.prefs_path.write_text(json.dumps(self.prefs, ensure_ascii=False), encoding="utf-8")

    def save_session(self) -> None:
        if self._closing:
            return
        registered = {t["name"] for t in self.tools}
        # 遠隔からつないでいる間は、画面の外ではなく元の大きさ・位置を覚える (落ちても次は画面の上に開くように)
        geometry = self._remote_geometry or self.root.geometry()
        data = {"geometry": geometry, "tabs": [t.name for t in self.tabs if t.name in registered],
                "active": self.active.name if self.active else None}
        self.session_path.parent.mkdir(parents=True, exist_ok=True)
        self.session_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    @property
    def hwnd(self) -> int:
        return user32.GetAncestor(self.root.winfo_id(), GA_ROOT)

    # ── 取り込み / 手放し ──
    def tab_of_exe(self, exe: str) -> Tab | None:
        return next((t for t in self.tabs if t.exe == os.path.normcase(exe)), None)

    def launch(self, tool: dict, new: bool = False) -> None:
        """ツールを開く。new=False なら、取り込み済みはそのタブへ切り替え、起動中 (ドックの外) は取り込む。

        取り込んだ窓はタイトルバーを外して隠しているので find_window では見つからない。
        タブの側で exe を覚えておかないと、2 つ目を起動してしまう (試験で 1 → 2 になった)
        """
        exe = tool["exe"]
        hwnd = None
        if not new:
            tab = self.tab_of_exe(exe)
            if tab:
                self.select(tab)
                return
            hwnd = find_window(set(), exe)
        if hwnd is None:
            proc = subprocess.Popen([exe], cwd=str(Path(exe).parent))
            deadline = time.time() + 20
            while hwnd is None and time.time() < deadline:
                self.root.update()
                time.sleep(0.2)
                tree = process_tree(proc.pid)
                if any(t.pid in tree for t in self.tabs):
                    return  # 待っている間に自動取り込みがタブにした (取り込んだ窓は隠すので find_window では見えない)
                hwnd = find_window(tree)
        if hwnd is None:
            print(f"[WinTabDock] {tool['name']} の窓が見つかりません", file=sys.stderr)
            return
        self.adopt(tool["name"], hwnd)

    def adopt(self, name: str, hwnd: int) -> None:
        if any(t.hwnd == hwnd for t in self.tabs) or hwnd == self.hwnd:
            return
        # 最大化した窓は、ドックが大きさと位置を決めても、画面いっぱいのまま動かない (VS Code の新しい窓は最大化して開く。
        # 画面いっぱいのまま重なり、スマホでは帯ごと隠れた)。最大化を解いてから控え、外すときに最大化に戻す
        maximized = False
        for _ in range(2):  # 最大化してから最小化した窓は、1 回目で最大化に戻る
            if not (user32.IsIconic(hwnd) or user32.IsZoomed(hwnd)):
                break
            maximized = maximized or bool(user32.IsZoomed(hwnd))
            user32.ShowWindow(hwnd, SW_RESTORE)
        maximized = maximized or bool(user32.IsZoomed(hwnd))
        style = user32.GetWindowLongPtrW(hwnd, GWL_STYLE)
        exstyle = user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
        pid = window_pid(hwnd)
        tab = Tab(name, hwnd, style, exstyle, rect_of(hwnd), exe=os.path.normcase(process_path(pid)),
                  owner=user32.GetWindowLongPtrW(hwnd, GWLP_HWNDPARENT), pid=pid, maximized=maximized)
        self.tabs.append(tab)
        save_state(self.tabs)  # 変える前に控える
        user32.ShowWindow(hwnd, SW_HIDE)
        # 所有者をドックにする。前面の奪い合いで重なり順を直す作りでは、ドックを押した瞬間 (移動・リサイズ・
        # ボタン) に本体が真っ暗になり、他のアプリの後ろにも回った。所有される窓は Windows が所有者より手前に置く
        user32.SetWindowLongPtrW(hwnd, GWLP_HWNDPARENT, self.hwnd)
        user32.SetWindowLongPtrW(hwnd, GWL_STYLE, style & ~(WS_CAPTION | WS_THICKFRAME))
        user32.SetWindowLongPtrW(hwnd, GWL_EXSTYLE, (exstyle | WS_EX_TOOLWINDOW) & ~WS_EX_APPWINDOW)
        user32.SetWindowPos(hwnd, None, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE | SWP_FRAMECHANGED)
        tab.button = tk.Button(self.tabs_frame, text=name, relief=tk.FLAT, padx=12,
                               command=lambda t=tab: self.select(t))
        tab.button.bind("<Button-2>", lambda e, t=tab: self.close_tab(t))  # 中クリックでツールを閉じる
        tab.button.bind("<Button-3>", lambda e, t=tab: self.tab_menu(e, t))
        tab.button.pack(side=tk.LEFT, padx=(4, 0), pady=4)
        tab.button_hwnd = tab.button.winfo_id()
        self.empty.place_forget()
        self.select(tab)

    def release(self, tab: Tab) -> None:
        self.ignored.add(tab.hwnd)  # 元に戻すと窓が現れるので、自動取り込みがすぐ取り込み直してしまう
        restore_window(tab.hwnd, tab.style, tab.exstyle, tab.rect, tab.owner, tab.maximized)
        self.forget(tab)

    def forget(self, tab: Tab) -> None:
        if tab.button:
            tab.button.destroy()
        self.tabs.remove(tab)
        save_state(self.tabs)
        if self.active is tab:
            self.active = None
            if self.tabs:
                self.select(self.tabs[-1])
            else:
                self.empty.place(relx=0.5, rely=0.5, anchor=tk.CENTER)
        self.save_session()

    def close_tab(self, tab: Tab) -> None:
        user32.PostMessageW(tab.hwnd, WM_CLOSE, 0, 0)  # 閉じたら poll が気づいてタブを消す

    # ── 表示 ──
    def select(self, tab: Tab) -> None:
        self.close_launch_panel(show=False)  # 一覧を開いたままタブを押した
        for t in self.tabs:
            on = t is tab
            t.button.configure(bg=TAB_ACTIVE if on else TAB_BG, fg=TAB_FG if on else TAB_FG_DIM,
                               font=("", 9, "bold" if on else "normal"))
            if not on:
                user32.ShowWindow(t.hwnd, SW_HIDE)
        self.active = tab
        self.place_active(show=True)
        self.save_session()
        view = self.remote_server.view if self.remote_server else None
        if view and view.requested:
            # 遠隔からつないでいる間は、切り替えたツールに合わせて大きさを合わせ直す (最小の幅のあるツールなら縦に伸ばす)。
            # 合わせ直しは Tk のスレッドの仕事を待つので、別のスレッドから頼む
            threading.Thread(target=view.resize, args=view.requested, daemon=True).start()

    def body_rect(self) -> tuple[int, int, int, int]:
        self.root.update_idletasks()
        return self.body.winfo_rootx(), self.body.winfo_rooty(), self.body.winfo_width(), self.body.winfo_height()

    def place_active(self, show: bool = False) -> None:
        if not self.active:
            return
        x, y, w, h = self.body_rect()
        if user32.IsZoomed(self.active.hwnd):
            # タブにしたあとでツールが自分で最大化した (VS Code は開いた少しあとで前回の最大化に戻す)。最大化のままでは
            # 位置と大きさを決めても動かない。前に出さずに解いてから置く。外すときは最大化に戻す
            self.active.maximized = True
            user32.ShowWindow(self.active.hwnd, SW_SHOWNOACTIVATE)
        user32.SetWindowPos(self.active.hwnd, None, x, y, w, h, SWP_NOZORDER | SWP_NOACTIVATE)
        if show:
            user32.ShowWindow(self.active.hwnd, SW_SHOWNOACTIVATE)
            # 所有者を付け替えた直後や、隠していた窓を出した直後は、Windows がまだ重なり順を組み直していない
            # (実験では、ドックの重なり順を一度動かすとツールが手前に来た)
            user32.SetWindowPos(self.hwnd, HWND_TOP, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)
            if user32.GetForegroundWindow() == self.hwnd:
                # 人がドックを操作した直後なら、キー入力がツールに届くようにする (前面のプロセスからなら許される)
                user32.SetForegroundWindow(self.active.hwnd)

    def after_layout(self) -> None:
        if not self._layout_pending:
            self._layout_pending = True
            self.root.after(15, self._do_layout)

    def _do_layout(self) -> None:
        self._layout_pending = False
        if self.root.state() != "iconic":
            self.place_active()

    def on_minimize(self) -> None:
        if self.active:
            user32.ShowWindow(self.active.hwnd, SW_HIDE)

    def on_restore(self) -> None:
        self.place_active(show=True)

    def poll(self) -> None:
        for t in list(self.tabs):
            if not user32.IsWindow(t.hwnd):
                self.forget(t)
        if (self.active and user32.IsZoomed(self.active.hwnd) and self.root.state() != "iconic"
                and not self.launch_panel):  # 一覧を開いている間は隠している (置き直すと出てしまう)
            self.remote_log(f"「{self.active.name}」が最大化したので、解いてドックに収め直した")
            self.place_active()  # 表示中のツールが自分で最大化した。最大化を解いて置き直す
        self.root.after(500, self.poll)

    # ── メニュー ──
    def show_menu(self) -> None:
        self.build_menu().tk_popup(self.root.winfo_rootx() + self.root.winfo_width() - 200, self.bar.winfo_rooty() + 36)

    def build_menu(self) -> tk.Menu:
        m = tk.Menu(self.root, tearoff=False)
        for tool in self.tools:
            label = f"{tool['name']} に切り替え" if self.tab_of_exe(tool["exe"]) else f"起動: {tool['name']}"
            m.add_command(label=label, command=lambda t=tool: self.launch(t))
        m.add_separator()
        sub = tk.Menu(m, tearoff=False)
        mine = {t.hwnd for t in self.tabs} | {self.hwnd}
        for hwnd in top_windows():
            if hwnd not in mine:
                title = window_title(hwnd)
                sub.add_command(label=title[:60], command=lambda h=hwnd, n=title: self.adopt(n[:20], h))
        m.add_cascade(label="起動中の窓を取り込む", menu=sub)
        # 2 つ目を起動することはまれなので、ふだんの項目と分けて一番下に置く
        m.add_separator()
        again = tk.Menu(m, tearoff=False)
        for tool in self.tools:
            again.add_command(label=tool["name"], command=lambda t=tool: self.launch(t, new=True))
        m.add_cascade(label="もう 1 つ起動", menu=again)
        return m

    def show_settings(self) -> None:
        b = self.settings_button
        self.build_settings_menu().tk_popup(b.winfo_rootx() + b.winfo_width() - 220, b.winfo_rooty() + b.winfo_height())

    def build_settings_menu(self) -> tk.Menu:
        """設定は「+ 起動 / 取り込む」と分ける (一緒のメニューだと、ふだんの操作と設定が混ざって分かりにくかった)。"""
        m = tk.Menu(self.root, tearoff=False)
        m.add_cascade(label="起動時に開く", menu=self.build_startup_menu(m))
        on_close = tk.Menu(m, tearoff=False)
        self._on_close_var = tk.StringVar(value=self.on_close_mode)
        for mode, label in self.ON_CLOSE_MODES.items():
            on_close.add_radiobutton(label=label, value=mode, variable=self._on_close_var,
                                     command=lambda: self.set_on_close(self._on_close_var.get()))
        m.add_cascade(label="閉じるときのツール", menu=on_close)
        if FROZEN:  # ソースから動かしているときは、登録する起動コマンドが定まらないので出さない
            self._autostart_var = tk.BooleanVar(value=autostart_enabled())
            m.add_checkbutton(label="ログイン時に起動する", variable=self._autostart_var,
                              command=lambda: set_autostart(self._autostart_var.get()))
        m.add_separator()
        self._remote_var = tk.BooleanVar(value=self.remote_server is not None)
        m.add_checkbutton(label="スマホから使う (ブラウザで開く)", variable=self._remote_var,
                          command=lambda: self.start_remote() if self._remote_var.get() else self.stop_remote())
        if self.remote_server:
            s = self.remote_server
            m.add_command(label=f"    {s.host}:{s.port} で待っています", state=tk.DISABLED)  # 状態の表示 (押せない)
            m.add_command(label="    つなぐ URL をコピー", command=self.copy_remote_url)
        m.add_command(label="    スマホから使う: アドレスと状態...", command=self.show_remote_panel)
        m.add_separator()
        m.add_command(label=f"WinTabDock {__version__}", state=tk.DISABLED)  # 版の表示 (押せない)
        return m

    def build_startup_menu(self, parent: tk.Menu) -> tk.Menu:
        """起動時に開くもの: 前回開いていたもの / 選んだものだけ (ツールごとのチェック) / 何も開かない。"""
        sub = tk.Menu(parent, tearoff=False)
        self._startup_var = tk.StringVar(value=self.startup_mode)
        for mode, label in self.STARTUP_MODES.items():
            sub.add_radiobutton(label=label, value=mode, variable=self._startup_var, command=self.on_startup_mode)
        sub.add_separator()
        chosen = set(self.prefs.get("startup_tools", []))
        state = tk.NORMAL if self.startup_mode == "selected" else tk.DISABLED
        self._startup_tool_vars = {}
        for tool in self.tools:
            var = tk.BooleanVar(value=tool["name"] in chosen)
            self._startup_tool_vars[tool["name"]] = var
            sub.add_checkbutton(label=tool["name"], variable=var, state=state, command=self.on_startup_tools)
        return sub

    def on_startup_mode(self) -> None:
        self.prefs["startup"] = self._startup_var.get()
        if self.prefs["startup"] == "selected" and "startup_tools" not in self.prefs:
            # 初めて「選んだものだけ」にしたときは、いま開いているタブを選んだ状態から始める
            self.prefs["startup_tools"] = [t.name for t in self.tabs if self.tool_by_name(t.name)]
        self.save_prefs()

    def on_startup_tools(self) -> None:
        self.prefs["startup_tools"] = [n for n, v in self._startup_tool_vars.items() if v.get()]
        self.save_prefs()

    # ── 遠隔 (スマホのブラウザから使う。remote.py) ──
    def call_in_tk(self, fn, wait: bool = True, timeout: float = 5.0) -> None:
        """別のスレッドから、Tk のスレッドで fn を動かす (Tk は作ったスレッドからしか触れない)。"""
        done = threading.Event()
        self._tk_calls.put((fn, done))

        def nudge() -> bool:
            # 呼び出しは 20ms ごとの時計 (after) で動くので、1 回起こしただけでは時計の前にまた眠ることがある。
            # 動くまで 50ms ごとに起こし続ける (遠隔を切ったあとの quit が届かずドックが閉じなかった、2026-10-27)
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                self._wake_tk()
                if done.wait(0.05):
                    return True
            return False

        if not wait:
            threading.Thread(target=nudge, daemon=True).start()
        elif not nudge():
            self.remote_log(f"Tk のスレッドが {timeout} 秒応えない ({getattr(fn, '__name__', fn)})")

    def _wake_tk(self) -> None:
        """Tk のスレッドを起こす (ドックの窓に知らせを 1 つ置く)。
        遠隔からつないでいる間、Tk が GetMessage で眠ったまま、時計 (after) の知らせが来なくなることがあった
        (数秒から 18 秒。別のスレッドからの tkinter の呼び出しも戻らない)。知らせを 1 つ置くと動き出した。
        原因は確かめきれていない (入力の状態の共有 AttachThreadInput と、ドックが持ち主のツールの窓が絡むと見ている)"""
        if self.dock_hwnd:
            user32.PostMessageW(self.dock_hwnd, 0, 0, 0)  # WM_NULL

    def _keep_tk_awake(self) -> None:
        """つないでいる間 (ドックが画面の外にある間)、50ms ごとに Tk を起こす。"""
        while self._remote_geometry and not self._closing:
            self._wake_tk()
            time.sleep(0.05)

    def _drain_tk_calls(self) -> None:
        while not self._tk_calls.empty():
            fn, done = self._tk_calls.get()
            try:
                fn()
            except Exception as e:  # 1 つの失敗で、この後の頼み事まで止めない (次の予約をせずに抜けて、以後すべて届かなかった)
                self.remote_log(f"Tk のスレッドでの処理に失敗: {getattr(fn, '__name__', fn)}: {e!r}")
            finally:
                done.set()
        self.root.after(20, self._drain_tk_calls)

    def remote_log(self, msg: str) -> None:
        line = time.strftime("%Y-%m-%d %H:%M:%S ") + msg + "\n"
        try:
            with open(STATE_DIR / "remote.log", "a", encoding="utf-8") as f:
                f.write(line)
        except OSError:
            pass

    def remote_host(self) -> tuple[str, str]:
        """待ち受けるアドレスと、その決まり方。wintabdock.toml の host > 画面で選んだネットワーク > Tailscale > 127.0.0.1。
        選んだものはアドレスでなくネットワークの名前 (Wi-Fi など) で覚える。ルーターがアドレスを変えても、その時のアドレスで待つ"""
        if self.remote_settings.get("host"):
            return self.remote_settings["host"], "wintabdock.toml の [remote] host"
        chosen = self.prefs.get("remote_network")
        if chosen == "loopback":
            return "127.0.0.1", "この PC だけ"
        if chosen:
            for name, ip in remote.local_addresses():
                if name == chosen:
                    return ip, chosen
            return "127.0.0.1", f"「{chosen}」がつながっていないので、この PC だけ"
        ts = remote.tailscale_ip()
        return (ts, "Tailscale") if ts else ("127.0.0.1", "この PC だけ (まだ選んでいない)")

    def start_remote(self) -> None:
        """待ち受けを始める。noVNC の取得 (初回はダウンロード) は別のスレッドで行い、Tk を止めない。"""
        if self.remote_server or self._remote_starting:
            return
        host, self._remote_how = self.remote_host()
        port = int(self.remote_settings.get("port", remote.DEFAULT_PORT))
        token = self.prefs.get("remote_token") or remote.new_token()
        self.prefs.update(remote=True, remote_token=token)
        self.save_prefs()
        self.root.update_idletasks()
        self.dock_hwnd = self.hwnd
        self._remote_starting = True

        def prepare() -> None:
            try:
                web_root = remote.novnc_dir(STATE_DIR)
                tls = remote.tls_setup(STATE_DIR / "tls", host) if self.prefs.get("remote_https") else None
                view = remote.View(RemoteSource(self), self.remote_zoom)
                server = remote.Server(view, host, port, token, web_root, self.remote_log, tls)
                server.start()
            except OSError as e:
                # 文字にしてから渡す (e は except を抜けると消えるので、あとで動く lambda からは読めない)
                msg = f"{host}:{port} で待ち受けられませんでした。\n{e}"
                self.remote_log(msg.replace("\n", " "))
                self.call_in_tk(lambda: self.remote_failed(msg), wait=False)
                return
            self.remote_log(f"待ち受けを始めた {host}:{port}" + (" (HTTPS)" if tls else ""))
            self.call_in_tk(lambda: self._remote_started(server), wait=False)

        threading.Thread(target=prepare, daemon=True).start()

    def _remote_started(self, server: remote.Server) -> None:
        self._remote_starting = False
        if not self.prefs.get("remote") or self._closing:  # 準備している間に止められた
            server.stop()
            return
        self.remote_server = server

    def remote_failed(self, msg: str) -> None:
        from tkinter import messagebox
        self._remote_starting = False
        self.prefs["remote"] = False
        self.save_prefs()
        panel = self._remote_panel
        messagebox.showerror("WinTabDock", msg, parent=panel if panel and panel.winfo_exists() else self.root)

    def restart_remote(self) -> None:
        """アドレスや合言葉を変えたときに、待ち受けをやり直す (つないでいるスマホは切れ、自動でつなぎ直してくる)。"""
        if self.remote_server:
            self.remote_server.stop()
            self.remote_server = None
        if self._remote_geometry:
            self.remote_connected(False)
        if self.prefs.get("remote"):
            self.start_remote()

    def stop_remote(self) -> None:
        self.prefs["remote"] = False
        self.save_prefs()
        if self.remote_server:
            self.remote_server.stop()
            self.remote_server = None
        if self._remote_geometry:
            self.remote_connected(False)

    def copy_remote_url(self) -> None:
        if self.remote_server:
            self.root.clipboard_clear()
            self.root.clipboard_append(self.remote_server.url)

    def renew_remote_token(self) -> None:
        self.prefs["remote_token"] = remote.new_token()
        self.save_prefs()
        if self.remote_server:
            self.restart_remote()

    def set_remote_https(self, on: bool) -> None:
        """通信を暗号化するか (スマホに証明書を一度入れてもらう)。待ち受けていればやり直す。"""
        self.prefs["remote_https"] = on
        self.save_prefs()
        if self.remote_server:
            self.restart_remote()

    def set_remote_network(self, name: str) -> None:
        """待ち受けるネットワークを選ぶ (name はネットワークの名前か "loopback")。待ち受けていればやり直す。"""
        self.prefs["remote_network"] = name
        self.save_prefs()
        if self.remote_server:
            self.restart_remote()

    def show_remote_panel(self) -> None:
        """スマホから使うための画面。待ち受けるアドレスを選び、待ち受けの状態・つないでいる相手・URL を見る。"""
        if self._remote_panel and self._remote_panel.winfo_exists():
            self._remote_panel.deiconify()
            self._remote_panel.lift()
            return
        win = tk.Toplevel(self.root)
        win.title("スマホから使う")
        win.transient(self.root)
        win.resizable(False, False)
        self._remote_panel = win
        hand = {"cursor": "hand2"}
        primary = {"bg": TAB_ACTIVE, "fg": "#ffffff", "activebackground": "#5b21b6", "activeforeground": "#ffffff",
                   "relief": tk.RAISED, "bd": 1, "padx": 12, **hand}  # 主役は塗る

        # ボタンの行を先に下へ置く (本体を先に置くと、ボタンが画面の外に押し出される)
        bar = tk.Frame(win)
        bar.pack(side=tk.BOTTOM, fill=tk.X, padx=12, pady=10)
        tk.Button(bar, text="閉じる", width=10, command=win.destroy, **hand).pack(side=tk.RIGHT)

        # 右に QR コード (スマホのカメラで読めば URL をコピーして送らずに開ける)。出し入れで画面の大きさが変わらないよう幅を決めておく
        qr_box = tk.Frame(win, width=260, height=345)
        qr_box.pack_propagate(False)
        qr_box.pack(side=tk.RIGHT, fill=tk.Y, padx=(0, 12), pady=(10, 0))
        qr_mode = tk.StringVar(value="connect")  # HTTPS のとき、QR を「つなぐ URL」と「証明書の入れ方」で切り替える
        qr_tabs = tk.Frame(qr_box)
        for value, text in (("connect", "つなぐ"), ("setup", "証明書を入れる")):
            tk.Radiobutton(qr_tabs, text=text, value=value, variable=qr_mode, indicatoron=False, padx=8, pady=2,
                           selectcolor="#ddd6fe", command=lambda: refresh(), **hand).pack(side=tk.LEFT, padx=2)
        qr_label = tk.Label(qr_box, bd=0)
        qr_label.pack(side=tk.TOP)
        qr_note = tk.Label(qr_box, justify=tk.CENTER, fg="#555555", wraplength=250)
        qr_note.pack(side=tk.TOP, pady=(6, 0))
        qr_shown = [None]  # いま出している QR の URL

        body = tk.Frame(win)
        body.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=12, pady=(10, 0))
        head = tk.Frame(body)
        head.pack(fill=tk.X)
        status = tk.Label(head, anchor=tk.W, justify=tk.LEFT, font=("", 10, "bold"))
        status.pack(side=tk.LEFT, fill=tk.X, expand=True)
        toggle = tk.Button(head, width=10, **primary)
        toggle.pack(side=tk.RIGHT, padx=(10, 0))
        peers = tk.Label(body, anchor=tk.W, justify=tk.LEFT, fg="#555555")
        peers.pack(fill=tk.X, pady=(2, 10))

        tk.Label(body, text="待ち受けるアドレス (スマホがつなぐ先)", anchor=tk.W, font=("", 9, "bold")).pack(fill=tk.X)
        choices = tk.Frame(body)
        choices.pack(fill=tk.X, padx=(8, 0))
        hint = tk.Label(body, anchor=tk.W, justify=tk.LEFT, fg="#555555", wraplength=440)
        hint.pack(fill=tk.X, pady=(4, 10))

        tk.Label(body, text="つなぐ URL (合言葉が入っています。人に渡さないでください)", anchor=tk.W,
                 font=("", 9, "bold")).pack(fill=tk.X)
        url_row = tk.Frame(body)
        url_row.pack(fill=tk.X, pady=(2, 0))
        url_var = tk.StringVar()
        tk.Entry(url_row, textvariable=url_var, state="readonly", width=48).pack(side=tk.LEFT, fill=tk.X, expand=True)
        copy = tk.Button(url_row, text="コピー", width=8, command=self.copy_remote_url, **hand)
        copy.pack(side=tk.LEFT, padx=(6, 0))
        renew = tk.Button(body, text="合言葉を作り直す (前の URL は使えなくなる)", command=self.renew_remote_token, **hand)
        renew.pack(anchor=tk.W, pady=(6, 0))
        port = int(self.remote_settings.get("port", remote.DEFAULT_PORT))
        tk.Label(body, text=f"ポート {port} (変えるときは wintabdock.toml の [remote] port)", anchor=tk.W,
                 fg="#555555").pack(fill=tk.X, pady=(6, 0))
        zoom_info = tk.Label(body, anchor=tk.W, fg="#555555")
        zoom_info.pack(fill=tk.X)
        https_var = tk.BooleanVar(value=bool(self.prefs.get("remote_https")))
        tk.Checkbutton(body, text="通信を暗号化する (HTTPS。スマホに証明書を一度入れる)", variable=https_var, anchor=tk.W,
                       command=lambda: self.set_remote_https(https_var.get()), **hand).pack(fill=tk.X, pady=(8, 0))
        https_info = tk.Label(body, anchor=tk.W, justify=tk.LEFT, fg="#555555", wraplength=440)
        https_info.pack(fill=tk.X)

        net_var = tk.StringVar()
        shown: list = []  # 前に並べたアドレス (変わったときだけ並べ直す)

        def fill_choices(fixed: bool) -> None:
            nets = [(n, ip) for n, ip in remote.local_addresses()]
            key = (fixed, nets)
            if shown and shown[0] == key:
                return
            shown[:] = [key]
            for w in choices.winfo_children():
                w.destroy()
            state = tk.DISABLED if fixed else tk.NORMAL
            for name, ip in nets:
                label = f"{name}  {ip}" + ("  (Tailscale。外からも暗号化してつなげる)" if remote.is_tailscale(ip) else "")
                tk.Radiobutton(choices, text=label, value=name, variable=net_var, state=state, anchor=tk.W,
                               command=lambda n=name: self.set_remote_network(n), **hand).pack(fill=tk.X)
            tk.Radiobutton(choices, text="この PC だけ  127.0.0.1  (スマホからはつながらない)", value="loopback",
                           variable=net_var, state=state, anchor=tk.W,
                           command=lambda: self.set_remote_network("loopback"), **hand).pack(fill=tk.X)

        def refresh() -> None:
            if not win.winfo_exists():
                return
            host, how = (self.remote_server.host, self._remote_how) if self.remote_server else self.remote_host()
            fixed = bool(self.remote_settings.get("host"))
            fill_choices(fixed)
            # 印は、いま待ち受けているアドレスの行に付ける (toml で決めているときも。空だと Tk は全部を「どれとも言えない」の印にする)
            net_var.set("loopback" if host == "127.0.0.1" else
                        next((n for n, ip in remote.local_addresses() if ip == host), self.prefs.get("remote_network", "-")))
            if self.remote_server:
                status.config(text=f"待ち受け中  {host}:{self.remote_server.port}", fg="#15803d")
                toggle.config(text="止める", command=self.stop_remote)
                who = self.remote_server.peers()
                peers.config(text="つないでいる: " + "、".join(who) if who else "つないでいるスマホはありません")
            elif self._remote_starting:
                status.config(text="始めています... (初回は noVNC をダウンロードします)", fg="#555555")
                toggle.config(text="止める", command=self.stop_remote)
                peers.config(text="")
            else:
                status.config(text="止まっています", fg="#b91c1c")
                toggle.config(text="始める", command=self.start_remote)
                peers.config(text="")
            notes = [f"いまのアドレス: {host} ({how})"]
            if fixed:
                notes.append("wintabdock.toml で決めているので、ここでは選べません。選ぶときは toml の host を消してください。")
            if host == "127.0.0.1":
                notes.append("この PC の中からしかつながりません。スマホから使うときは Wi-Fi か Tailscale を選んでください。")
            elif remote.is_tailscale(host):
                notes.append("Tailscale の中だけからつながります。通信は暗号化されます。")
            else:
                notes.append("同じネットワーク (家の Wi-Fi など) からつながります。" +
                             ("通信は HTTPS で暗号化します。" if https_var.get() else
                              "通信は暗号化されないので、同じ Wi-Fi の人には合言葉ごと見えることがあります"
                              " (下の「通信を暗号化する」で防げます)。") +
                             "初めて待ち受けるとき、Windows のファイアウォールの確認が出たら「プライベート ネットワーク」で"
                             "許可してください。")
            hint.config(text="\n".join(notes))
            tls = self.remote_server.tls if self.remote_server else None
            if tls:
                https_info.config(text="初めてのスマホには、右の「証明書を入れる」の QR から証明書を入れてください。"
                                       f"\n証明書の指紋 (SHA-256): {tlscert.fingerprint(tls[1])}")
            elif https_var.get():
                https_info.config(text="待ち受けを始めると、証明書を作ります。")
            else:
                https_info.config(text="")
            if tls and host != "127.0.0.1":
                qr_tabs.pack(side=tk.TOP, before=qr_label, pady=(0, 6))
            else:
                qr_tabs.pack_forget()
                qr_mode.set("connect")
            zoom_info.config(text=f"スマホでの表示倍率 {round(self.remote_zoom * 100)}% "
                                  "(スマホで、タブの帯の「− 縮小」「+ 拡大」で変える)")
            on = self.remote_server is not None
            url_var.set(self.remote_server.url.replace(self.remote_server.token, "********") if on else "(待ち受けていません)")
            copy.config(state=tk.NORMAL if on else tk.DISABLED)
            renew.config(state=tk.NORMAL if on else tk.DISABLED)
            setup = qr_mode.get() == "setup"
            want = "" if not on or host == "127.0.0.1" else \
                self.remote_server.setup_url if setup else self.remote_server.url
            if want != qr_shown[0]:
                qr_shown[0] = want
                qr_label.image = qr_photo(want) if want else None  # 画像は手元で持っておかないと消える
                qr_label.config(image=qr_label.image or "")
            qr_note.config(text="証明書の入れ方のページが開きます。\n入れたら「つなぐ」の QR を読んでください" if want and setup else
                           "スマホのカメラで読むと開きます。\n合言葉が入っているので、人に見せないでください" if want else
                           "この PC だけで待ち受けているので、\nQR コードは出しません" if on else
                           "待ち受けを始めると、\nここに QR コードが出ます")
            win.after(1000, refresh)

        refresh()
        win.update_idletasks()
        x = self.root.winfo_rootx() + max(0, (self.root.winfo_width() - win.winfo_width()) // 2)
        y = self.root.winfo_rooty() + 40
        win.geometry(f"+{x}+{y}")

    def remote_connected(self, on: bool) -> None:
        """つないでいる間はドックを画面の外 (右の外) に置く。手元のほかの窓が重なっていると、Tk のボタンは
        窓へのメッセージで押せない (WindowFromPoint で見ているらしい)。切れたら元の大きさ・位置に戻す。"""
        if on and not self._remote_geometry:
            if self.root.state() == "iconic":
                self.root.deiconify()
            self._remote_geometry = self.root.geometry()
            self._remote_maxsize = self.root.maxsize()
            # 縮めて見せるときは画面より高いドックが要る。Tk の既定 (画面の大きさ) のままだと、Windows が画面の高さで止める
            self.root.maxsize(8000, 8000)
            # 遠隔からは押せない「+ 起動」「設定」を外し、表示倍率のボタンを出す
            self.menu_button.pack_forget()
            self.settings_frame.pack_forget()
            self.show_zoom()
            # タブより先に場所を取る (タブが多いと、後から並べた帯の右端のボタンが切れて押せなかった)
            self.zoom_frame.pack(side=tk.RIGHT, before=self.tabs_frame)
            self.open_frame.pack(side=tk.LEFT, padx=(8, 4), pady=4, before=self.tabs_frame)
            self.empty.config(text="左上の「+ 開く」からツールを開きます")
            x = user32.GetSystemMetrics(76) + user32.GetSystemMetrics(78) + 100  # 仮想画面の右端の外
            self.root.geometry(f"+{x}+{user32.GetSystemMetrics(77)}")
            threading.Thread(target=self._keep_tk_awake, daemon=True).start()
            self.show_remote_notice()
            self.remote_log("つながったので、ドックを画面の外に置いた")
        elif not on and self._remote_geometry:
            if self._remote_notice and self._remote_notice.winfo_exists():
                self._remote_notice.destroy()
            self._remote_notice = None
            geometry, self._remote_geometry = self._remote_geometry, None
            self.close_launch_panel(show=False)  # 下で選んでいるツールを出し直す
            self.empty.config(text="右上の「+ 起動 / 取り込む」からツールを開きます")
            self.zoom_frame.pack_forget()
            self.open_frame.pack_forget()
            self.pack_bar_buttons()
            self.root.maxsize(*self._remote_maxsize)
            self.root.geometry(geometry)
            self.root.update_idletasks()
            self.place_active(show=True)
            # ドックの上へ移したツールの小窓を元の位置へ戻す。閉じて隠れているだけの小窓も (次に開くと前の位置に出る)
            moved, self.remote_moved = self.remote_moved, {}
            for hwnd, (x, y) in moved.items():
                if user32.IsWindow(hwnd):
                    user32.SetWindowPos(hwnd, None, x, y, 0, 0, SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE)
            self.remote_log("切れたので、ドックを元の場所に戻した")

    ZOOMS = (0.5, 0.67, 0.75, 0.9, 1.0, 1.25, 1.5)  # スマホでの表示倍率の段

    @property
    def remote_zoom(self) -> float:
        z = self.prefs.get("remote_zoom", 1.0)
        return z if z in self.ZOOMS else 1.0

    def change_zoom(self, step: int) -> None:
        """表示倍率を 1 段変える (スマホからタブの帯のボタンで)。覚えておき、次につないだときも使う。"""
        i = max(0, min(len(self.ZOOMS) - 1, self.ZOOMS.index(self.remote_zoom) + step))
        self.prefs["remote_zoom"] = self.ZOOMS[i]
        self.save_prefs()
        self.show_zoom()
        view = self.remote_server.view if self.remote_server else None
        if view:
            view.zoom = self.ZOOMS[i]
            if view.requested:
                # 大きさを合わせ直す。resize は Tk のスレッドに頼んで待つので、Tk のスレッドからは別のスレッドで呼ぶ
                threading.Thread(target=view.resize, args=view.requested, daemon=True).start()
        self.remote_log(f"表示倍率を {round(self.ZOOMS[i] * 100)}% にした")

    def show_zoom(self) -> None:
        z = self.remote_zoom
        self.zoom_label.config(text=f"{round(z * 100)}%")
        self.zoom_out_button.config(state=tk.DISABLED if z == self.ZOOMS[0] else tk.NORMAL)
        self.zoom_in_button.config(state=tk.DISABLED if z == self.ZOOMS[-1] else tk.NORMAL)

    def toggle_launch_panel(self) -> None:
        if self.launch_panel:
            self.close_launch_panel()
        else:
            self.open_launch_panel()

    def open_launch_panel(self) -> None:
        """スマホの「+ 開く」: ツールの起動・切り替えと、選んでいるツールを閉じるボタンを、ドックの中に並べる。
        ツールの窓はドックより手前にあるので、開いている間は隠す (遠隔の画面にも写らない)"""
        if self.active:
            user32.ShowWindow(self.active.hwnd, SW_HIDE)
        panel = tk.Frame(self.body, bg="#0d0d1a", padx=16, pady=12)
        panel.place(relx=0, rely=0, relwidth=1, relheight=1)
        self.launch_panel = panel
        button = {"relief": tk.FLAT, "fg": TAB_FG, "font": ("", 11), "anchor": tk.W, "padx": 12, "pady": 8,
                  "cursor": "hand2"}
        buttons = []

        def add(text: str, action, bg: str = TAB_BG, active_bg: str = TAB_ACTIVE, top: int = 0) -> None:
            # 押した時の処理は Tk に後でやってもらう。起動は窓が出るまで待つので、遠隔の「離した」が返ってこなくなる。
            # 一覧を消して選んでいたツールを出し直してから動かす (起動に失敗しても、隠したままにならない)
            b = tk.Button(panel, text=text, bg=bg, activebackground=active_bg, activeforeground=TAB_FG,
                          command=lambda: self.root.after(0, lambda: (self.close_launch_panel(), action())),
                          **button)
            b.pack(fill=tk.X, pady=(top, 6))
            buttons.append(b)

        def heading(text: str, top: int = 0) -> None:  # 押せない見出し (枠も塗りも無い小さな文字)
            tk.Label(panel, text=text, bg="#0d0d1a", fg=TAB_FG_DIM, anchor=tk.W).pack(fill=tk.X, pady=(top, 6))

        if self.tabs:
            # スマホの幅では帯にタブが並びきらず、右のタブが切れる。ここからも切り替えられるようにする
            heading("開いているツール")
            for t in self.tabs:
                on = t is self.active  # 帯のタブと同じく、表示中は紫で塗る
                add(f"{t.name} (表示中)" if on else t.name, lambda t=t: self.select(t), bg=TAB_ACTIVE if on else TAB_BG,
                    active_bg="#6d28d9" if on else TAB_ACTIVE)
        heading("起動する", top=12 if self.tabs else 0)
        closed = [tool for tool in self.tools if not self.tab_of_exe(tool["exe"])]
        for tool in closed:
            add(f"起動: {tool['name']}", lambda t=tool: self.launch(t))
        if not closed:
            heading("登録したツールはすべて開いています" if self.tools
                    else "登録したツールがありません (wintabdock.toml の [[tools]])")
        add("戻る", lambda: None, top=12)
        tab = self.active
        if tab:
            # 閉じるのは取り返しがつかないので、一番下にほかと離して置き、色も変える
            add(f"「{tab.name}」を閉じる", lambda: self.close_tab(tab), bg="#4a1d2e", active_bg="#7f1d1d", top=24)
        self.launch_hwnds = {b.winfo_id() for b in buttons}

    def close_launch_panel(self, show: bool = True) -> None:
        """「+ 開く」の一覧を消す。show なら隠していたツールを出し直す (ツールを選び直すときは、選ぶ側が出す)"""
        if not self.launch_panel:
            return
        self.launch_panel.destroy()
        self.launch_panel = None
        self.launch_hwnds = set()
        if show:
            self.place_active(show=True)

    def pack_bar_buttons(self) -> None:
        """帯の右端に「設定」と「+ 起動 / 取り込む」を置く (side=RIGHT は先に置いたものほど右に並ぶ)。"""
        self.settings_frame.pack(side=tk.RIGHT, padx=(0, 8), pady=4)
        self.menu_button.pack(side=tk.RIGHT, padx=6, pady=4)

    def show_remote_notice(self) -> None:
        """つないでいる間、PC の画面の右下に出す小さな窓。ドックが画面の外にいることを知らせ、PC の側から取り戻せるようにする。
        閉じられない (閉じると取り戻す手段が無くなる)。「PC で使う」は待ち受けごと止める (接続を切るだけだと、
        noVNC がすぐにつなぎ直してきて、ドックがまた画面の外に出る)"""
        win = tk.Toplevel(self.root)
        win.title("WinTabDock")
        win.attributes("-toolwindow", True, "-topmost", True)  # タスクバーにボタンを増やさない。ほかの窓の後ろに隠れない
        win.resizable(False, False)
        win.protocol("WM_DELETE_WINDOW", lambda: None)
        self._remote_notice = win
        tk.Label(win, text="スマホで使用中", font=("", 10, "bold"), anchor=tk.W).pack(fill=tk.X, padx=12, pady=(10, 0))
        who = tk.Label(win, anchor=tk.W, justify=tk.LEFT, fg="#555555")
        who.pack(fill=tk.X, padx=12)
        tk.Label(win, text="ドックは画面の外に移しています。\nスマホを切ると、元の場所に戻ります。", anchor=tk.W,
                 justify=tk.LEFT, fg="#555555").pack(fill=tk.X, padx=12, pady=(4, 8))
        tk.Button(win, text="PC で使う (スマホから使うのを止める)", command=self.stop_remote, bg=TAB_ACTIVE, fg="#ffffff",
                  activebackground="#5b21b6", activeforeground="#ffffff", relief=tk.RAISED, bd=1, padx=12,
                  cursor="hand2").pack(fill=tk.X, padx=12, pady=(0, 12))

        def refresh() -> None:
            if not win.winfo_exists():
                return
            peers = self.remote_server.peers() if self.remote_server else []
            who.config(text="つないでいる: " + ("、".join(peers) or "-"))
            win.after(1000, refresh)

        refresh()
        win.update_idletasks()
        area = wintypes.RECT()
        user32.SystemParametersInfoW(0x30, 0, ctypes.byref(area), 0)  # SPI_GETWORKAREA (主の画面の、タスクバーを除いた所)
        win.geometry(f"+{area.right - win.winfo_width() - 24}+{area.bottom - win.winfo_height() - 60}")

    def remote_resize(self, w: int, h: int) -> None:
        """遠隔の画面の大きさに、ドックの内側を合わせる (ツールは place_active でついてくる)。"""
        if self.root.state() == "iconic":
            self.root.deiconify()
        self.root.geometry(f"{max(200, w)}x{max(120, h)}")
        # 窓の大きさが変わった知らせ (Configure) を処理してから並べる。update_idletasks だけでは中身がまだ古い大きさで、
        # ツールを前の幅のまま置き、遠隔の側が「ツールが収まらない」と取り違えてドックを縦に伸ばした
        self.root.update()
        self.place_active()
        self.remote_log(f"ドックの内側を {w}x{h} にした → {remote.client_size(self.dock_hwnd)}")

    # ── 閉じるとき ──
    ON_CLOSE_MODES = {"ask": "毎回たずねる", "close": "一緒に閉じる", "keep": "元の窓に戻して残す"}
    CLOSE_WAIT_S = 5  # 「一緒に閉じる」でツールが閉じ終わるのを待つ長さ

    @property
    def on_close_mode(self) -> str:
        mode = self.prefs.get("on_close")
        return mode if mode in self.ON_CLOSE_MODES else "ask"

    def set_on_close(self, mode: str) -> None:
        self.prefs["on_close"] = mode
        self.save_prefs()

    def request_close(self) -> None:
        """ドックの × から。取り込んでいるツールを閉じるか残すかは、設定 (または毎回たずねる) に従う。"""
        if self._closing:
            return  # ツールが閉じ終わるのを待っている間に、もう一度 × を押された
        if not self.tabs or self.on_close_mode == "keep":
            self.quit()
        elif self.on_close_mode == "close":
            self.close_tools_and_quit()
        else:
            self.ask_close()

    def ask_close(self) -> None:
        win = tk.Toplevel(self.root)
        win.title("WinTabDock を閉じる")
        win.transient(self.root)
        win.resizable(False, False)
        remember = tk.BooleanVar(value=False)

        def choose(mode: str | None) -> None:
            win.grab_release()
            win.destroy()
            if mode is None:
                return
            if remember.get():
                self.set_on_close(mode)
            self.close_tools_and_quit() if mode == "close" else self.quit()

        # ボタンの行を先に下へ置く (本体を先に置くと、ボタンが画面の外に押し出される)
        bar = tk.Frame(win)
        bar.pack(side=tk.BOTTOM, fill=tk.X, padx=10, pady=10)
        self._close_buttons = {
            "close": tk.Button(bar, text="ツールも閉じる", width=14, command=lambda: choose("close")),
            "keep": tk.Button(bar, text="残して閉じる", width=14, command=lambda: choose("keep")),
            "cancel": tk.Button(bar, text="キャンセル", width=10, command=lambda: choose(None)),
        }
        self._close_buttons["cancel"].pack(side=tk.RIGHT)
        self._close_buttons["keep"].pack(side=tk.RIGHT, padx=(0, 6))
        self._close_buttons["close"].pack(side=tk.RIGHT, padx=(0, 6))
        self._close_remember = tk.Checkbutton(win, text="次からたずねない (メニューの「閉じるときのツール」で戻せる)",
                                              variable=remember)
        self._close_remember.pack(side=tk.BOTTOM, anchor=tk.W, padx=10)
        names = "、".join(t.name for t in self.tabs)
        tk.Label(win, text=f"取り込んでいるツール ({names}) も閉じますか？\n残す場合は、元の窓に戻ります。",
                 justify=tk.LEFT).pack(side=tk.TOP, anchor=tk.W, padx=12, pady=(12, 6))
        win.protocol("WM_DELETE_WINDOW", lambda: choose(None))
        win.update_idletasks()
        x = self.root.winfo_rootx() + (self.root.winfo_width() - win.winfo_width()) // 2
        y = self.root.winfo_rooty() + (self.root.winfo_height() - win.winfo_height()) // 3
        win.geometry(f"+{x}+{y}")
        self._close_dialog = win
        win.grab_set()
        self._close_buttons["keep"].focus_set()

    def close_tools_and_quit(self) -> None:
        """取り込んでいるツールに閉じるメッセージ (× と同じ) を送り、閉じ終わるのを待ってからドックを閉じる。
        待っても閉じないもの (保存の確認を出しているなど) は元の窓に戻して残す。確認の画面ごと消さないため。"""
        self.save_session()  # 閉じる前の並びを覚える (次回「前回開いていたもの」で開けるように)
        self._closing = True  # 以降、タブが減るたびに前回の並びを上書きしない。自動取り込みもしない
        for t in list(self.tabs):
            user32.PostMessageW(t.hwnd, WM_CLOSE, 0, 0)
        deadline = time.time() + self.CLOSE_WAIT_S

        def wait() -> None:
            for t in list(self.tabs):
                if not user32.IsWindow(t.hwnd):
                    self.forget(t)
            if self.tabs and time.time() < deadline:
                self.root.after(200, wait)
            else:
                self.quit()  # 残ったものは元の窓に戻す

        self.root.after(200, wait)

    def tool_by_name(self, name: str) -> dict | None:
        return next((t for t in self.tools if t["name"] == name), None)

    def tab_menu(self, event, tab: Tab) -> None:
        m = tk.Menu(self.root, tearoff=False)
        m.add_command(label="ドックから外す (窓は残す)", command=lambda: self.release(tab))
        m.add_command(label="ツールを閉じる", command=lambda: self.close_tab(tab))
        m.tk_popup(event.x_root, event.y_root)

    def quit(self) -> None:
        self.save_session()  # 窓を戻す前に並びを覚える (戻すたびに保存すると空になる)
        self._closing = True
        if self.remote_server:
            self.remote_server.stop()
            self.remote_server = None
        for t in list(self.tabs):
            self.release(t)
        if STATE.exists():
            STATE.unlink()
        if getattr(self, "_hook_thread_id", 0):
            user32.PostThreadMessageW(self._hook_thread_id, WM_QUIT, 0, 0)
        self.root.destroy()


def load_config() -> tuple[list[dict], dict]:
    """(ツール, 設定)。設定は auto_adopt (既定 true) と restore_session (既定 true)。"""
    path = APP_DIR / "wintabdock.toml"
    data = tomllib.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    return data.get("tool", []), {k: v for k, v in data.items() if k != "tool"}


def main() -> None:
    if sys.stdout:  # 窓なしの exe では sys.stdout が None (ここで落ちて exe が起動しなかった)
        sys.stdout.reconfigure(encoding="utf-8")
    ctypes.windll.shcore.SetProcessDpiAwareness(2)  # 座標を物理ピクセルでそろえる (相手の窓と合わせるため)
    if "--rescue" in sys.argv:
        print(f"{rescue(include_legacy=True)} 個の窓を元に戻しました")
        return
    n = rescue()  # 前回落ちたときに隠れたままの窓を戻す
    if n:
        print(f"[WinTabDock] 前回の窓を {n} 個元に戻しました")
    tools, settings = load_config()
    if "--selftest" not in sys.argv:
        Dock(tools, settings).root.mainloop()
        return
    from selftest import MODES, run
    args = sys.argv[sys.argv.index("--selftest") + 1:]
    names = args[1:] if args[:1] and args[0] in MODES else args
    # 試験のドックが扱うのは試験の対象だけ。人が使っている他のツールを自動で取り込まないように。
    # 前回のタブも、人のもの (session.json) とは別のファイルを使う
    tools = [t for t in tools if t["name"] in names]
    mode = args[0] if args[:1] and args[0] in MODES else None
    session_path, prefs_path = STATE_DIR / "session-selftest.json", STATE_DIR / "settings-selftest.json"
    if mode not in ("session-restore", "startup-check"):
        # 前の試験が残した前回のタブ・設定 (大きさ・位置など) に左右されないように
        session_path.unlink(missing_ok=True)
        prefs_path.unlink(missing_ok=True)
    settings = {**settings, "auto_adopt": True, "restore_session": mode in ("session-restore", "startup-check")}
    dock = Dock(tools, settings, session_path=session_path, prefs_path=prefs_path)
    run(dock, args)
    dock.root.mainloop()


if __name__ == "__main__":
    main()
