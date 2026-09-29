# /// script
# requires-python = ">=3.11"
# dependencies = ["pillow"]
# ///
"""ToolDock (試作): 別々に起動した exe の窓を 1 つの枠にタブでまとめる。

    uv run tooldock.py              # tooldock.toml のツールを並べたドックを開く
    uv run tooldock.py --rescue     # 落ちたドックが隠したままの窓を元に戻す
    uv run tooldock.py --selftest LPortMan PortWho   # 自動の確認 (docs/ に画像を書く)

仕組み (仮想タブ): 相手の窓はトップレベルのまま、枠のタイトル・縁を外してドックの本体の上に重ねる。
窓の所有者 (owner) をドックにするので、Windows が常にツールをドックより手前に置き、最小化も一緒になり、
タスクバーのボタンも出ない。選んでいないタブの窓は隠す。
取り込んだ窓の元の状態は state.json に控え、ドックが落ちても次の起動か --rescue で戻す
(ドックが落ちても、所有者が外れるだけで相手の窓とプロセスは残る。experiments/owner_probe.py で確認)。
"""

from __future__ import annotations

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

# ── Win32 ────────────────────────────────────────────────────────────────────

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

GWL_STYLE, GWL_EXSTYLE, GWLP_HWNDPARENT = -16, -20, -8  # トップレベルの窓では HWNDPARENT が所有者
WS_CAPTION, WS_THICKFRAME = 0x00C00000, 0x00040000
WS_MINIMIZEBOX, WS_MAXIMIZEBOX, WS_SYSMENU = 0x00020000, 0x00010000, 0x00080000
WS_EX_TOOLWINDOW, WS_EX_APPWINDOW = 0x00000080, 0x00040000
SW_HIDE, SW_SHOWNOACTIVATE, SW_SHOW, SW_RESTORE = 0, 4, 5, 9
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
# 控えと前回のタブの置き場所。TOOLDOCK_HOME で変えられる (exe を試すとき、人の前回のタブに触れないように)
STATE_DIR = Path(os.environ.get("TOOLDOCK_HOME") or Path(os.environ.get("LOCALAPPDATA", ".")) / "ToolDock")

# exe (PyInstaller) では、設定 (tooldock.toml) は exe の隣、同梱したアイコンは _MEIPASS にある
FROZEN = bool(getattr(sys, "frozen", False))
APP_DIR = Path(sys.executable).resolve().parent if FROZEN else Path(__file__).resolve().parent
RES_DIR = Path(getattr(sys, "_MEIPASS", APP_DIR))
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def autostart_enabled() -> bool:
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            return winreg.QueryValueEx(k, "ToolDock")[0] == f'"{sys.executable}"'
    except OSError:
        return False


def set_autostart(on: bool) -> None:
    """ログイン時に起動する (HKCU の Run)。登録するのはいま動いている exe の場所なので、移動したら入れ直す。"""
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if on:
            winreg.SetValueEx(k, "ToolDock", 0, winreg.REG_SZ, f'"{sys.executable}"')
        else:
            try:
                winreg.DeleteValue(k, "ToolDock")
            except FileNotFoundError:
                pass
LEGACY_STATE = STATE_DIR / "state.json"  # 以前の共有の控え。--rescue でだけ扱う


def state_path(pid: int | None = None) -> Path:
    return STATE_DIR / f"state-{pid or os.getpid()}.json"


STATE = state_path()  # このドックの控え


def save_state(tabs: list["Tab"]) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps([{"hwnd": t.hwnd, "style": t.style, "exstyle": t.exstyle, "rect": t.rect,
                                  "owner": t.owner} for t in tabs]), encoding="utf-8")


def other_docks() -> list[int]:
    """このプロセス以外で開いているドックの窓。"""
    return [h for h in top_windows() if window_title(h) == "ToolDock" and window_pid(h) != os.getpid()]


def restore_window(hwnd: int, style: int, exstyle: int, rect: tuple[int, int, int, int], owner: int = 0) -> None:
    if not user32.IsWindow(hwnd):
        return
    user32.ShowWindow(hwnd, SW_HIDE)  # 拡張スタイルの変更をタスクバーに反映させるため、一度隠す
    user32.SetWindowLongPtrW(hwnd, GWLP_HWNDPARENT, owner)
    user32.SetWindowLongPtrW(hwnd, GWL_STYLE, style)
    user32.SetWindowLongPtrW(hwnd, GWL_EXSTYLE, exstyle)
    x, y, w, h = rect
    user32.SetWindowPos(hwnd, None, x, y, w, h, SWP_NOZORDER | SWP_NOACTIVATE | SWP_FRAMECHANGED)
    user32.ShowWindow(hwnd, SW_SHOW)


def _restore_file(path: Path) -> int:
    n = 0
    for s in json.loads(path.read_text(encoding="utf-8")):
        if user32.IsWindow(s["hwnd"]):
            restore_window(s["hwnd"], s["style"], s["exstyle"], tuple(s["rect"]), s.get("owner", 0))
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


TAB_BG, TAB_ACTIVE, TAB_FG, TAB_FG_DIM, BAR_BG = "#252542", "#7c3aed", "#ffffff", "#9a9ab8", "#1a1a2e"


class Dock:
    def __init__(self, tools: list[dict], settings: dict | None = None, session_path: Path | None = None):
        settings = settings or {}
        self.tools = tools
        self.auto_adopt = settings.get("auto_adopt", True)
        self.restore_session = settings.get("restore_session", True)
        self.session_path = session_path or STATE_DIR / "session.json"
        self.ignored: set[int] = set()  # 「ドックから外す」で外した窓。すぐ自動で取り込み直さない
        self._closing = False
        self.tabs: list[Tab] = []
        self.active: Tab | None = None
        self.root = tk.Tk()
        self.root.title("ToolDock")
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
        self.menu_button = tk.Button(self.bar, text="+ 起動 / 取り込む", command=self.show_menu, bg=TAB_BG, fg=TAB_FG,
                                     relief=tk.FLAT, activebackground=TAB_ACTIVE, padx=10)
        self.menu_button.pack(side=tk.RIGHT, padx=6, pady=4)
        self.body = tk.Frame(self.root, bg="#0d0d1a")
        self.body.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        self.empty = tk.Label(self.body, text="右上の「+ 起動 / 取り込む」からツールを開きます",
                              bg="#0d0d1a", fg=TAB_FG_DIM)
        self.empty.place(relx=0.5, rely=0.5, anchor=tk.CENTER)
        self.root.protocol("WM_DELETE_WINDOW", self.quit)
        self.root.bind("<Configure>", lambda e: self.after_layout())
        self.root.bind("<Unmap>", lambda e: self.on_minimize() if e.widget is self.root else None)
        self.root.bind("<Map>", lambda e: self.on_restore() if e.widget is self.root else None)
        self._layout_pending = False
        self.root.after(500, self.poll)
        if self.auto_adopt:
            # 窓が現れたことはフックで受ける。フックは別スレッドで受け、キューで渡す
            # (tkinter の mainloop と同じスレッドで ctypes のコールバックを受けると Python ごと落ちた)
            self._shown: queue.SimpleQueue[int] = queue.SimpleQueue()
            self._hook_thread_id = 0
            threading.Thread(target=self._watch_windows, daemon=True).start()
            self.root.after(150, self._drain_shown)
        self.root.after(300, lambda: self.startup(session))

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
            self.root.after(400, lambda h=hwnd: self.maybe_adopt(h))
        self.root.after(150, self._drain_shown)

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

    def startup(self, session: dict) -> None:
        """前回のタブを開き直し (起動中なら取り込み、なければ起動)、ほかに起動中の登録済みツールも取り込む。"""
        if self.restore_session:
            for name in session.get("tabs", []):
                tool = next((t for t in self.tools if t["name"] == name), None)
                if tool:
                    self.launch(tool)
        if self.auto_adopt:
            for hwnd in top_windows():
                self.maybe_adopt(hwnd)
        active = next((t for t in self.tabs if t.name == session.get("active")), None) if self.restore_session else None
        if active:
            self.select(active)

    def load_session(self) -> dict:
        try:
            return json.loads(self.session_path.read_text(encoding="utf-8")) if self.restore_session else {}
        except (OSError, ValueError):
            return {}

    def save_session(self) -> None:
        if self._closing:
            return
        registered = {t["name"] for t in self.tools}
        data = {"geometry": self.root.geometry(), "tabs": [t.name for t in self.tabs if t.name in registered],
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
            print(f"[ToolDock] {tool['name']} の窓が見つかりません", file=sys.stderr)
            return
        self.adopt(tool["name"], hwnd)

    def adopt(self, name: str, hwnd: int) -> None:
        if any(t.hwnd == hwnd for t in self.tabs) or hwnd == self.hwnd:
            return
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, SW_RESTORE)
        style = user32.GetWindowLongPtrW(hwnd, GWL_STYLE)
        exstyle = user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
        pid = window_pid(hwnd)
        tab = Tab(name, hwnd, style, exstyle, rect_of(hwnd), exe=os.path.normcase(process_path(pid)),
                  owner=user32.GetWindowLongPtrW(hwnd, GWLP_HWNDPARENT), pid=pid)
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
        self.empty.place_forget()
        self.select(tab)

    def release(self, tab: Tab) -> None:
        self.ignored.add(tab.hwnd)  # 元に戻すと窓が現れるので、自動取り込みがすぐ取り込み直してしまう
        restore_window(tab.hwnd, tab.style, tab.exstyle, tab.rect, tab.owner)
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
        for t in self.tabs:
            on = t is tab
            t.button.configure(bg=TAB_ACTIVE if on else TAB_BG, fg=TAB_FG if on else TAB_FG_DIM,
                               font=("", 9, "bold" if on else "normal"))
            if not on:
                user32.ShowWindow(t.hwnd, SW_HIDE)
        self.active = tab
        self.place_active(show=True)
        self.save_session()

    def body_rect(self) -> tuple[int, int, int, int]:
        self.root.update_idletasks()
        return self.body.winfo_rootx(), self.body.winfo_rooty(), self.body.winfo_width(), self.body.winfo_height()

    def place_active(self, show: bool = False) -> None:
        if not self.active:
            return
        x, y, w, h = self.body_rect()
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
        if FROZEN:  # ソースから動かしているときは、登録する起動コマンドが定まらないので出さない
            m.add_separator()
            self._autostart_var = tk.BooleanVar(value=autostart_enabled())
            m.add_checkbutton(label="ログイン時に起動する", variable=self._autostart_var,
                              command=lambda: set_autostart(self._autostart_var.get()))
        return m

    def tab_menu(self, event, tab: Tab) -> None:
        m = tk.Menu(self.root, tearoff=False)
        m.add_command(label="ドックから外す (窓は残す)", command=lambda: self.release(tab))
        m.add_command(label="ツールを閉じる", command=lambda: self.close_tab(tab))
        m.tk_popup(event.x_root, event.y_root)

    def quit(self) -> None:
        self.save_session()  # 窓を戻す前に並びを覚える (戻すたびに保存すると空になる)
        self._closing = True
        for t in list(self.tabs):
            self.release(t)
        if STATE.exists():
            STATE.unlink()
        if getattr(self, "_hook_thread_id", 0):
            user32.PostThreadMessageW(self._hook_thread_id, WM_QUIT, 0, 0)
        self.root.destroy()


def load_config() -> tuple[list[dict], dict]:
    """(ツール, 設定)。設定は auto_adopt (既定 true) と restore_session (既定 true)。"""
    path = APP_DIR / "tooldock.toml"
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
        print(f"[ToolDock] 前回の窓を {n} 個元に戻しました")
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
    settings = {**settings, "auto_adopt": True, "restore_session": args[:1] == ["session-restore"]}
    dock = Dock(tools, settings, session_path=STATE_DIR / "session-selftest.json")
    run(dock, args)
    dock.root.mainloop()


if __name__ == "__main__":
    main()
