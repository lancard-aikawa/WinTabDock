# /// script
# requires-python = ">=3.11"
# dependencies = ["pillow"]
# ///
"""ToolDock (試作): 別々に起動した exe の窓を 1 つの枠にタブでまとめる。

    uv run tooldock.py              # tooldock.toml のツールを並べたドックを開く
    uv run tooldock.py --rescue     # 落ちたドックが隠したままの窓を元に戻す
    uv run tooldock.py --selftest LPortMan PortWho   # 自動の確認 (docs/ に画像を書く)

仕組み (仮想タブ): 相手の窓はトップレベルのまま、枠のタイトル・縁を外してドックの本体の上に重ねる。
選んでいないタブの窓は隠し、タスクバーのボタンは WS_EX_TOOLWINDOW で消す。
窓の所有者 (owner) は変えない。変えるとドックが終わるときに相手の窓まで壊れるおそれがある。
取り込んだ窓の元の状態は state.json に控え、ドックが落ちても次の起動か --rescue で戻す。
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

GWL_STYLE, GWL_EXSTYLE = -16, -20
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


def process_tree(root_pid: int) -> set[int]:
    """root_pid とその子孫。PyInstaller の onefile は子プロセスが窓を出すため。"""
    snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    children: dict[int, list[int]] = {}
    entry = PROCESSENTRY32W()
    entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
    ok = kernel32.Process32FirstW(snap, ctypes.byref(entry))
    while ok:
        children.setdefault(entry.th32ParentProcessID, []).append(entry.th32ProcessID)
        ok = kernel32.Process32NextW(snap, ctypes.byref(entry))
    kernel32.CloseHandle(snap)
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

STATE = Path(os.environ.get("LOCALAPPDATA", ".")) / "ToolDock" / "state.json"


def save_state(tabs: list["Tab"]) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps([{"hwnd": t.hwnd, "style": t.style, "exstyle": t.exstyle, "rect": t.rect}
                                 for t in tabs]), encoding="utf-8")


def restore_window(hwnd: int, style: int, exstyle: int, rect: tuple[int, int, int, int]) -> None:
    if not user32.IsWindow(hwnd):
        return
    user32.ShowWindow(hwnd, SW_HIDE)  # 拡張スタイルの変更をタスクバーに反映させるため、一度隠す
    user32.SetWindowLongPtrW(hwnd, GWL_STYLE, style)
    user32.SetWindowLongPtrW(hwnd, GWL_EXSTYLE, exstyle)
    x, y, w, h = rect
    user32.SetWindowPos(hwnd, None, x, y, w, h, SWP_NOZORDER | SWP_NOACTIVATE | SWP_FRAMECHANGED)
    user32.ShowWindow(hwnd, SW_SHOW)


def rescue() -> int:
    if not STATE.exists():
        return 0
    n = 0
    for s in json.loads(STATE.read_text(encoding="utf-8")):
        if user32.IsWindow(s["hwnd"]):
            restore_window(s["hwnd"], s["style"], s["exstyle"], tuple(s["rect"]))
            n += 1
    STATE.unlink()
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


TAB_BG, TAB_ACTIVE, TAB_FG, TAB_FG_DIM, BAR_BG = "#252542", "#7c3aed", "#ffffff", "#9a9ab8", "#1a1a2e"


class Dock:
    def __init__(self, tools: list[dict]):
        self.tools = tools
        self.tabs: list[Tab] = []
        self.active: Tab | None = None
        self.root = tk.Tk()
        self.root.title("ToolDock")
        self.root.geometry("1100x720+80+60")
        self.root.configure(bg=BAR_BG)
        self.bar = tk.Frame(self.root, bg=BAR_BG, height=36)
        self.bar.pack(side=tk.TOP, fill=tk.X)
        self.tabs_frame = tk.Frame(self.bar, bg=BAR_BG)
        self.tabs_frame.pack(side=tk.LEFT, fill=tk.Y)
        tk.Button(self.bar, text="+ 起動 / 取り込む", command=self.show_menu, bg=TAB_BG, fg=TAB_FG, relief=tk.FLAT,
                  activebackground=TAB_ACTIVE, padx=10).pack(side=tk.RIGHT, padx=6, pady=4)
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
        # 前景の窓が変わったら重なり順を直す (ツールをクリック → ドックをその直下へ / ドック → ツールを上へ)。
        # フックは別スレッドで受ける。tkinter の mainloop 中に同じスレッドで ctypes のコールバックが走ると
        # 「PyEval_RestoreThread: the GIL is released」で Python ごと落ちる
        self._fg: queue.SimpleQueue[int] = queue.SimpleQueue()
        self._hook_thread_id = 0
        threading.Thread(target=self._hook_loop, daemon=True).start()
        self.root.after(30, self._drain_foreground)
        self.root.after(500, self.poll)

    def _hook_loop(self) -> None:
        self._hook_thread_id = kernel32.GetCurrentThreadId()
        proc = WINEVENTPROC(lambda _h, _e, hwnd, *_: self._fg.put(hwnd or 0))
        hook = user32.SetWinEventHook(EVENT_SYSTEM_FOREGROUND, EVENT_SYSTEM_FOREGROUND, None, proc, 0, 0,
                                      WINEVENT_OUTOFCONTEXT)
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        user32.UnhookWinEvent(hook)

    def _drain_foreground(self) -> None:
        last = None
        while not self._fg.empty():
            last = self._fg.get()
        if last:
            self.on_foreground(last)
        self.root.after(30, self._drain_foreground)

    @property
    def hwnd(self) -> int:
        return user32.GetAncestor(self.root.winfo_id(), GA_ROOT)

    # ── 取り込み / 手放し ──
    def launch(self, tool: dict) -> None:
        exe = tool["exe"]
        hwnd = find_window(set(), exe)  # 起動中ならそれを取り込む (2 つ目を起動しない)
        if hwnd is None:
            proc = subprocess.Popen([exe], cwd=str(Path(exe).parent))
            deadline = time.time() + 20
            while hwnd is None and time.time() < deadline:
                self.root.update()
                time.sleep(0.2)
                hwnd = find_window(process_tree(proc.pid))
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
        tab = Tab(name, hwnd, style, exstyle, rect_of(hwnd))
        self.tabs.append(tab)
        save_state(self.tabs)  # 変える前に控える
        user32.ShowWindow(hwnd, SW_HIDE)
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
        restore_window(tab.hwnd, tab.style, tab.exstyle, tab.rect)
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

    def body_rect(self) -> tuple[int, int, int, int]:
        self.root.update_idletasks()
        return self.body.winfo_rootx(), self.body.winfo_rooty(), self.body.winfo_width(), self.body.winfo_height()

    def place_active(self, show: bool = False) -> None:
        if not self.active:
            return
        x, y, w, h = self.body_rect()
        if show:
            user32.ShowWindow(self.active.hwnd, SW_SHOWNOACTIVATE)
        user32.SetWindowPos(self.active.hwnd, HWND_TOP, x, y, w, h, SWP_NOACTIVATE)
        # ツールを上げるだけでは、ドックが前景のときに Windows がドックを手前に戻すことがある (試験で見えなかった)。
        # 自分の窓は確実に動かせるので、ドックをツールの直下へ下げる
        user32.SetWindowPos(self.hwnd, self.active.hwnd, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)

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

    def on_foreground(self, hwnd: int) -> None:
        if not self.active or not hwnd:
            return
        if hwnd == self.active.hwnd:
            # ツールが前に来た → ドックをその直下へ (タブの帯が他の窓の後ろに残らないように)
            user32.SetWindowPos(self.hwnd, self.active.hwnd, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)
        elif hwnd == self.hwnd:
            user32.SetWindowPos(self.active.hwnd, HWND_TOP, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)

    def poll(self) -> None:
        for t in list(self.tabs):
            if not user32.IsWindow(t.hwnd):
                self.forget(t)
        self.root.after(500, self.poll)

    # ── メニュー ──
    def show_menu(self) -> None:
        m = tk.Menu(self.root, tearoff=False)
        for tool in self.tools:
            m.add_command(label=f"起動: {tool['name']}", command=lambda t=tool: self.launch(t))
        m.add_separator()
        sub = tk.Menu(m, tearoff=False)
        mine = {t.hwnd for t in self.tabs} | {self.hwnd}
        for hwnd in top_windows():
            if hwnd not in mine:
                title = window_title(hwnd)
                sub.add_command(label=title[:60], command=lambda h=hwnd, n=title: self.adopt(n[:20], h))
        m.add_cascade(label="起動中の窓を取り込む", menu=sub)
        m.tk_popup(self.root.winfo_rootx() + self.root.winfo_width() - 200, self.bar.winfo_rooty() + 36)

    def tab_menu(self, event, tab: Tab) -> None:
        m = tk.Menu(self.root, tearoff=False)
        m.add_command(label="ドックから外す (窓は残す)", command=lambda: self.release(tab))
        m.add_command(label="ツールを閉じる", command=lambda: self.close_tab(tab))
        m.tk_popup(event.x_root, event.y_root)

    def quit(self) -> None:
        for t in list(self.tabs):
            self.release(t)
        if STATE.exists():
            STATE.unlink()
        if self._hook_thread_id:
            user32.PostThreadMessageW(self._hook_thread_id, WM_QUIT, 0, 0)
        self.root.destroy()


def load_tools() -> list[dict]:
    path = Path(__file__).with_name("tooldock.toml")
    return tomllib.loads(path.read_text(encoding="utf-8")).get("tool", []) if path.exists() else []


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    ctypes.windll.shcore.SetProcessDpiAwareness(2)  # 座標を物理ピクセルでそろえる (相手の窓と合わせるため)
    if "--rescue" in sys.argv:
        print(f"{rescue()} 個の窓を元に戻しました")
        return
    n = rescue()  # 前回落ちたときに隠れたままの窓を戻す
    if n:
        print(f"[ToolDock] 前回の窓を {n} 個元に戻しました")
    dock = Dock(load_tools())
    if "--selftest" in sys.argv:
        from selftest import run
        run(dock, sys.argv[sys.argv.index("--selftest") + 1:])
    dock.root.mainloop()


if __name__ == "__main__":
    main()
