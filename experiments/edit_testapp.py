"""試験用の、Tk でない窓 (Windows の EDIT 部品だけ)。Mery など、BackSpace を WM_CHAR(8) で処理する作りの代わり。

    python experiments/edit_testapp.py <目印>

タイトルは "edit-testapp <目印>"。中の文字は外から WM_GETTEXT で読める。標準ライブラリだけ。
上の帯 (高さ BAR_H) を押すと、Mery のメニューの帯と同じく、押した時に TrackPopupMenu でメニューを出す。
「XYZ を挿入」は文字を足し、「その他」の下の「SUB を挿入」も文字を足す (メニューを遠隔から選べたかを中の文字で見る)
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
u.CreatePopupMenu.restype = wintypes.HMENU
u.AppendMenuW.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_size_t, wintypes.LPCWSTR]
u.TrackPopupMenu.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.HWND,
                             wintypes.LPVOID]
u.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
u.SendMessageW.restype = LRESULT
u.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
u.BeginPaint.argtypes = [wintypes.HWND, ctypes.c_void_p]
u.EndPaint.argtypes = [wintypes.HWND, ctypes.c_void_p]
u.TextOutW = ctypes.windll.gdi32.TextOutW
u.TextOutW.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int, wintypes.LPCWSTR, ctypes.c_int]
k.GetModuleHandleW.restype = wintypes.HMODULE

WM_DESTROY, WM_SIZE, WM_SETFOCUS, WM_PAINT = 0x0002, 0x0005, 0x0007, 0x000F
WM_LBUTTONDOWN = 0x0201
EM_REPLACESEL = 0x00C2
WS_OVERLAPPEDWINDOW, WS_VISIBLE, WS_CHILD, WS_VSCROLL = 0x00CF0000, 0x10000000, 0x40000000, 0x00200000
ES_MULTILINE, ES_AUTOVSCROLL, ES_WANTRETURN = 0x0004, 0x0040, 0x1000
MF_STRING, MF_POPUP = 0x0000, 0x0010
TPM_RETURNCMD = 0x0100
BAR_H = 28
ID_XYZ, ID_SUB = 101, 102


class WNDCLASSW(ctypes.Structure):
    _fields_ = [("style", wintypes.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int), ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
                ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH), ("lpszMenuName", wintypes.LPCWSTR),
                ("lpszClassName", wintypes.LPCWSTR)]


def make_menu() -> int:
    menu = u.CreatePopupMenu()
    u.AppendMenuW(menu, MF_STRING, ID_XYZ, "XYZ を挿入")
    sub = u.CreatePopupMenu()
    u.AppendMenuW(sub, MF_STRING, ID_SUB, "SUB を挿入")
    u.AppendMenuW(menu, MF_POPUP, sub, "その他")
    return menu


def main() -> None:
    tag = sys.argv[1] if len(sys.argv) > 1 else "x"
    inst = k.GetModuleHandleW(None)
    edit = [0]
    menu = make_menu()

    def proc(hwnd, msg, wp, lp):
        if msg == WM_SIZE and edit[0]:
            u.MoveWindow(edit[0], 0, BAR_H, lp & 0xFFFF, (lp >> 16 & 0xFFFF) - BAR_H, True)
            return 0
        if msg == WM_SETFOCUS and edit[0]:
            u.SetFocus(edit[0])
            return 0
        if msg == WM_PAINT:
            ps = (ctypes.c_byte * 72)()
            dc = u.BeginPaint(hwnd, ps)
            u.TextOutW(dc, 8, 6, "ファイル", 4)
            u.EndPaint(hwnd, ps)
            return 0
        if msg == WM_LBUTTONDOWN and (lp >> 16 & 0xFFFF) < BAR_H:
            # Mery のメニューの帯と同じく、押した時に帯の下へメニューを出す (閉じるまでここから戻らない)
            pt = wintypes.POINT(0, BAR_H)
            u.ClientToScreen(hwnd, ctypes.byref(pt))
            cmd = u.TrackPopupMenu(menu, TPM_RETURNCMD, pt.x, pt.y, 0, hwnd, None)
            if cmd in (ID_XYZ, ID_SUB):
                text = ctypes.create_unicode_buffer("XYZ" if cmd == ID_XYZ else "SUB")
                u.SendMessageW(edit[0], EM_REPLACESEL, 1, ctypes.addressof(text))
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
                                | ES_WANTRETURN, 0, BAR_H, 480, 330, top, None, inst, None)
    u.SetFocus(edit[0])
    msg = wintypes.MSG()
    while u.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
        u.TranslateMessage(ctypes.byref(msg))
        u.DispatchMessageW(ctypes.byref(msg))


if __name__ == "__main__":
    main()
