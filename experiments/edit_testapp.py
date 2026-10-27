"""試験用の、Tk でない窓 (Windows の EDIT 部品だけ)。Mery など、BackSpace を WM_CHAR(8) で処理する作りの代わり。

    python experiments/edit_testapp.py <目印>

タイトルは "edit-testapp <目印>"。中の文字は外から WM_GETTEXT で読める。標準ライブラリだけ。
"""

import ctypes
import sys
from ctypes import wintypes

u = ctypes.WinDLL("user32", use_last_error=True)
k = ctypes.WinDLL("kernel32", use_last_error=True)
LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
u.DefWindowProcW.restype = LRESULT
u.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
u.CreateWindowExW.restype = wintypes.HWND
u.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_int,
                              ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.HWND, wintypes.HMENU,
                              wintypes.HINSTANCE, wintypes.LPVOID]
u.MoveWindow.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.BOOL]
u.SetFocus.argtypes = [wintypes.HWND]
u.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
u.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
u.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
k.GetModuleHandleW.restype = wintypes.HMODULE

WM_DESTROY, WM_SIZE, WM_SETFOCUS = 0x0002, 0x0005, 0x0007
WS_OVERLAPPEDWINDOW, WS_VISIBLE, WS_CHILD, WS_VSCROLL = 0x00CF0000, 0x10000000, 0x40000000, 0x00200000
ES_MULTILINE, ES_AUTOVSCROLL, ES_WANTRETURN = 0x0004, 0x0040, 0x1000


class WNDCLASSW(ctypes.Structure):
    _fields_ = [("style", wintypes.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int), ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
                ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH), ("lpszMenuName", wintypes.LPCWSTR),
                ("lpszClassName", wintypes.LPCWSTR)]


def main() -> None:
    tag = sys.argv[1] if len(sys.argv) > 1 else "x"
    inst = k.GetModuleHandleW(None)
    edit = [0]

    def proc(hwnd, msg, wp, lp):
        if msg == WM_SIZE and edit[0]:
            u.MoveWindow(edit[0], 0, 0, lp & 0xFFFF, lp >> 16 & 0xFFFF, True)
            return 0
        if msg == WM_SETFOCUS and edit[0]:
            u.SetFocus(edit[0])
            return 0
        if msg == WM_DESTROY:
            u.PostQuitMessage(0)
            return 0
        return u.DefWindowProcW(hwnd, msg, wp, lp)

    wndproc = WNDPROC(proc)
    wc = WNDCLASSW(lpfnWndProc=wndproc, hInstance=inst, hbrBackground=6, lpszClassName="EditTestApp")  # COLOR_WINDOW+1
    u.RegisterClassW(ctypes.byref(wc))
    top = u.CreateWindowExW(0, "EditTestApp", f"edit-testapp {tag}", WS_OVERLAPPEDWINDOW | WS_VISIBLE,
                            200, 200, 500, 400, None, None, inst, None)
    edit[0] = u.CreateWindowExW(0, "EDIT", "", WS_CHILD | WS_VISIBLE | WS_VSCROLL | ES_MULTILINE | ES_AUTOVSCROLL
                                | ES_WANTRETURN, 0, 0, 480, 360, top, None, inst, None)
    u.SetFocus(edit[0])
    msg = wintypes.MSG()
    while u.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
        u.TranslateMessage(ctypes.byref(msg))
        u.DispatchMessageW(ctypes.byref(msg))


if __name__ == "__main__":
    main()
