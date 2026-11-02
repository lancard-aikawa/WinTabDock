"""試験の窓が、人の使っている窓からフォーカスを奪わないようにする (標準ライブラリだけ。試験用の窓からも読み込む)。

試験から起動した窓は、試験が VS Code (前面) から起動されたので前面を取る許しを持ち、開くたびに前面に出た。
人が打っていたキーが試験のドックの「+ 拡大」に届き、倍率が狂って試験も落ちた (2026-11-02)。

- 自分のプロセスの窓が前面になったら、直前に前面だった別のプロセスの窓に返す。前面のプロセスからの
  SetForegroundWindow は断られない。ほかのプロセスから返そうと、前面のスレッドと入力の状態を共有 (AttachThreadInput)
  したら、ドックの遠隔の入力 (同じく共有してマウスのボタンの状態を書き換える) が乱れ、押した数が狂って試験が止まった
- LockSetForegroundWindow で、ほかのプロセスが前面を取るのも止める。人が Alt を押したり別の窓を押したりすると
  Windows が外し、前面のプロセスでないとかけ直せない (かけ直しは失敗するだけ)
- 環境変数 WINTABDOCK_TEST_NOFOCUS (試験のプロセスの番号) を付けて起動したプロセス (試験の子) は、start_if_asked で
  同じことをする

本物のマウスで押す試験 (selftest) では使わない。
"""

from __future__ import annotations

import atexit
import ctypes
import os
import threading
import time
from ctypes import wintypes

ENV = "WINTABDOCK_TEST_NOFOCUS"

_u = ctypes.WinDLL("user32")
_u.GetForegroundWindow.restype = wintypes.HWND
_u.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
_u.SetForegroundWindow.argtypes = [wintypes.HWND]
_u.IsWindow.argtypes = [wintypes.HWND]
_u.IsWindowVisible.argtypes = [wintypes.HWND]


def _pid(hwnd: int) -> int:
    pid = wintypes.DWORD()
    _u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


class _PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD),
                ("szExeFile", ctypes.c_wchar * 260)]


_k = ctypes.WinDLL("kernel32")
_k.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
_k.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
_k.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
_k.CloseHandle.argtypes = [wintypes.HANDLE]


def _tree(root: int) -> set[int]:
    """root とその子孫のプロセス"""
    snap = _k.CreateToolhelp32Snapshot(2, 0)  # TH32CS_SNAPPROCESS
    children: dict[int, list[int]] = {}
    e = _PROCESSENTRY32W()
    e.dwSize = ctypes.sizeof(e)
    ok = _k.Process32FirstW(snap, ctypes.byref(e))
    while ok:
        children.setdefault(e.th32ParentProcessID, []).append(e.th32ProcessID)
        ok = _k.Process32NextW(snap, ctypes.byref(e))
    _k.CloseHandle(snap)
    out, stack = set(), [root]
    while stack:
        p = stack.pop()
        if p not in out:
            out.add(p)
            stack += children.get(p, [])
    return out


def start(lock: bool = False) -> None:
    """自分の窓が前面になったら返す見張りを始める。lock なら、ほかのプロセスが前面を取るのも止める
    (試験を動かすプロセスで。子には環境変数で頼む)。
    返す先は、試験 (環境変数の番号のプロセス) とその子孫でない窓。ドックが返す先に、タブにした試験の窓を覚えていて、
    そこへ前面を渡していた。その間に送った「− 縮小」が効かなかった (前面が動くと、押している途中の操作が取り消される)"""
    me = os.getpid()
    root = int(os.environ.get(ENV) or me)
    os.environ[ENV] = str(root)

    def keep() -> None:
        user = last = 0  # user: 最後に前面にあった、試験のでない窓
        ours: set[int] = set()
        while True:
            if lock:
                _u.LockSetForegroundWindow(1)  # LSFW_LOCK
            fg = _u.GetForegroundWindow() or 0
            if fg != last:
                ours = _tree(root)  # 起動したばかりのプロセスの窓かもしれないので、変わるたびに数え直す
                last = fg
            pid = _pid(fg) if fg else 0
            if fg and pid not in ours:
                user = fg
            elif pid == me and user and _u.IsWindow(user) and _u.IsWindowVisible(user):
                _u.SetForegroundWindow(user)  # 返せるのは前面のプロセス (自分) だけ
            time.sleep(0.05)

    threading.Thread(target=keep, daemon=True).start()
    if lock:
        atexit.register(lambda: _u.LockSetForegroundWindow(2))  # LSFW_UNLOCK


def start_if_asked() -> None:
    if os.environ.get(ENV):
        start()
