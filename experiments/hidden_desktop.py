"""試験を、人の画面に出ない別のデスクトップ (CreateDesktop) で動かす (標準ライブラリだけ)。

試験の窓が前面を取ると、すぐ返しても、人が IME で変換している途中の文字がそこで確定・中断される (2026-11-02)。
別のデスクトップの窓は、人の画面の前面にもフォーカスにも触れない。画面を撮る (PrintWindow)・窓へのメッセージで押す・
打つ・クリップボードは、そこでも動いた (check_remote で 87 項目中 85。残りはエクスプローラーとクリップボードの取り合い)。
エクスプローラーは動かない (試験が応答を待ったまま止まった。上の部分のメニューは前面でないと開かないので、どのみち試せない)。

Win+Ctrl+D の仮想デスクトップは使えない: 内部は同じ 1 つのデスクトップで、新しい窓は人が見ている仮想デスクトップに開く。
"""

from __future__ import annotations

import ctypes
import msvcrt
import os
import subprocess
import sys
from ctypes import wintypes

NAME = "wintabdock-test"
ENV = "WINTABDOCK_TEST_DESKTOP"  # 別のデスクトップの中で動いている印

_u = ctypes.WinDLL("user32", use_last_error=True)
_k = ctypes.WinDLL("kernel32", use_last_error=True)
_u.CreateDesktopW.restype = wintypes.HANDLE
_u.CreateDesktopW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
                              ctypes.c_void_p]
_u.CloseDesktop.argtypes = [wintypes.HANDLE]
_k.CloseHandle.argtypes = [wintypes.HANDLE]
_k.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
_k.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]


class _STARTUPINFOW(ctypes.Structure):
    _fields_ = [("cb", wintypes.DWORD), ("lpReserved", wintypes.LPWSTR), ("lpDesktop", wintypes.LPWSTR),
                ("lpTitle", wintypes.LPWSTR), ("dwX", wintypes.DWORD), ("dwY", wintypes.DWORD),
                ("dwXSize", wintypes.DWORD), ("dwYSize", wintypes.DWORD), ("dwXCountChars", wintypes.DWORD),
                ("dwYCountChars", wintypes.DWORD), ("dwFillAttribute", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("wShowWindow", wintypes.WORD), ("cbReserved2", wintypes.WORD), ("lpReserved2", ctypes.c_void_p),
                ("hStdInput", wintypes.HANDLE), ("hStdOutput", wintypes.HANDLE), ("hStdError", wintypes.HANDLE)]


class _PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [("hProcess", wintypes.HANDLE), ("hThread", wintypes.HANDLE), ("dwProcessId", wintypes.DWORD),
                ("dwThreadId", wintypes.DWORD)]


def inside() -> bool:
    return bool(os.environ.get(ENV))


def rerun_inside() -> int:
    """このスクリプトを、同じ引数で別のデスクトップの中で動かし直す。出力はこちらに流し、終わりの番号を返す"""
    desk = _u.CreateDesktopW(NAME, None, None, 0, 0x10000000, None)  # GENERIC_ALL。同じ名前があればそれを開く
    if not desk:
        raise OSError(f"CreateDesktop に失敗 ({ctypes.get_last_error()})")
    read_fd, write_fd = os.pipe()
    write_h = msvcrt.get_osfhandle(write_fd)
    os.set_handle_inheritable(write_h, True)
    si = _STARTUPINFOW()
    si.cb = ctypes.sizeof(si)
    si.lpDesktop = NAME
    si.dwFlags = 0x00000100  # STARTF_USESTDHANDLES
    si.hStdOutput = si.hStdError = write_h
    pi = _PROCESS_INFORMATION()
    os.environ[ENV] = "1"
    line = subprocess.list2cmdline([sys.executable, *sys.argv])
    try:
        if not _k.CreateProcessW(None, ctypes.create_unicode_buffer(line), None, None, True, 0x08000000, None, None,
                                 ctypes.byref(si), ctypes.byref(pi)):  # CREATE_NO_WINDOW
            raise OSError(f"CreateProcess に失敗 ({ctypes.get_last_error()})")
    finally:
        del os.environ[ENV]
        os.close(write_fd)  # 子が閉じると読み終わる
    with os.fdopen(read_fd, "rb") as r:
        for chunk in iter(lambda: r.read1(4096), b""):
            sys.stdout.buffer.write(chunk)
            sys.stdout.buffer.flush()
    _k.WaitForSingleObject(pi.hProcess, 0xFFFFFFFF)
    code = wintypes.DWORD()
    _k.GetExitCodeProcess(pi.hProcess, ctypes.byref(code))
    _k.CloseHandle(pi.hProcess)
    _k.CloseHandle(pi.hThread)
    _u.CloseDesktop(desk)
    return code.value
