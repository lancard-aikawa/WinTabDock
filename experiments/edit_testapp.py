"""試験用の、Tk でない窓 (Windows の EDIT 部品だけ)。Mery など、BackSpace を WM_CHAR(8) で処理する作りの代わり。

    python experiments/edit_testapp.py <目印>

タイトルは "edit-testapp <目印>"。中の文字は外から WM_GETTEXT で読める。標準ライブラリだけ。
上の帯 (高さ BAR_H) を押すと、Mery のメニューの帯と同じく、押した時に TrackPopupMenu でメニューを出す。
「XYZ を挿入」は文字を足し、「その他」の下の「SUB を挿入」も文字を足す (メニューを遠隔から選べたかを中の文字で見る)。
Ctrl+S は、Mery などと同じくショートカットの表 (TranslateAccelerator) で受けて、"[S]" を足す。
「検索...」は、Mery の検索と同じく、この窓が持ち主の小窓 (タイトル "edit-testapp-find"、中に EDIT) を、画面の中に
収めて出す (この窓が画面の外にあれば、画面の右端へ寄せる)
環境変数 EDIT_TESTAPP_TABS があれば、下に Windows のタブの帯 (SysTabControl32、タブ 3 つ) を置く。Mery のタブの帯と
同じく、押すとタブを選んでから、隠れた窓にマウスをつかませる (引きずる用意)。つかむのが終わると、タブを外へ引き出した
ことにして、新しい窓 (タイトル "edit-testapp <目印> torn") を開く
"""

import ctypes
import os
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
u.CreateAcceleratorTableW.restype = wintypes.HANDLE
u.CreateAcceleratorTableW.argtypes = [ctypes.c_void_p, ctypes.c_int]
u.TranslateAcceleratorW.argtypes = [wintypes.HWND, wintypes.HANDLE, ctypes.POINTER(wintypes.MSG)]

WM_DESTROY, WM_SIZE, WM_SETFOCUS, WM_PAINT = 0x0002, 0x0005, 0x0007, 0x000F
WM_LBUTTONDOWN, WM_COMMAND = 0x0201, 0x0111
FVIRTKEY, FCONTROL = 0x01, 0x08
EM_REPLACESEL = 0x00C2
WS_OVERLAPPEDWINDOW, WS_VISIBLE, WS_CHILD, WS_VSCROLL = 0x00CF0000, 0x10000000, 0x40000000, 0x00200000
ES_MULTILINE, ES_AUTOVSCROLL, ES_WANTRETURN = 0x0004, 0x0040, 0x1000
MF_STRING, MF_POPUP = 0x0000, 0x0010
TPM_RETURNCMD = 0x0100
WS_POPUP, WS_CAPTION, WS_SYSMENU, WS_BORDER = 0x80000000, 0x00C00000, 0x00080000, 0x00800000
BAR_H = 28
ID_XYZ, ID_SUB, ID_FIND, ID_SAVE = 101, 102, 103, 104
u.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
u.SystemParametersInfoW.argtypes = [wintypes.UINT, wintypes.UINT, ctypes.c_void_p, wintypes.UINT]


class ACCEL(ctypes.Structure):
    _fields_ = [("fVirt", wintypes.BYTE), ("key", wintypes.WORD), ("cmd", wintypes.WORD)]


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
    u.AppendMenuW(menu, MF_STRING, ID_FIND, "検索...")
    return menu


def open_find(top: int, inst: int) -> None:
    """Mery の検索のような小窓。持ち主の窓の少し下に出すが、画面 (作業領域) の中に収める。"""
    r = wintypes.RECT()
    u.GetWindowRect(top, ctypes.byref(r))
    work = wintypes.RECT()
    u.SystemParametersInfoW(0x0030, 0, ctypes.byref(work), 0)  # SPI_GETWORKAREA
    w, h = 300, 120
    x = max(work.left, min(r.left, work.right - w))
    y = max(work.top, min(r.top + 14, work.bottom - h))
    dlg = u.CreateWindowExW(0, "EditTestFind", "edit-testapp-find", WS_POPUP | WS_CAPTION | WS_SYSMENU | WS_VISIBLE,
                            x, y, w, h, top, None, inst, None)
    box = u.CreateWindowExW(0, "EDIT", "", WS_CHILD | WS_VISIBLE | WS_BORDER, 10, 10, 260, 24, dlg, None, inst, None)
    u.SetFocus(box)


TAB_H = 30
TCM_INSERTITEMW = 0x133E
u.SetWindowLongPtrW.restype = ctypes.c_void_p
u.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_void_p]
u.CallWindowProcW.restype = LRESULT
u.CallWindowProcW.argtypes = [ctypes.c_void_p, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
u.SetCapture.argtypes = [wintypes.HWND]


class TCITEMW(ctypes.Structure):
    _fields_ = [("mask", wintypes.UINT), ("dwState", wintypes.DWORD), ("dwStateMask", wintypes.DWORD),
                ("pszText", wintypes.LPWSTR), ("cchTextMax", ctypes.c_int), ("iImage", ctypes.c_int),
                ("lParam", wintypes.LPARAM)]


def make_tabs(top: int, inst: int, tag: str) -> tuple[int, list]:
    """Mery のタブの帯の代わり (上の説明)。返すのは (タブの帯, 手放さずに持っておく物)。"""
    ctypes.windll.comctl32.InitCommonControls()
    tabs = u.CreateWindowExW(0, "SysTabControl32", "", WS_CHILD | WS_VISIBLE, 0, 0, 480, TAB_H, top, None, inst, None)
    for i, name in enumerate(("文書 1", "文書 2", "文書 3")):
        item = TCITEMW(mask=1, pszText=name)  # TCIF_TEXT
        u.SendMessageW(tabs, TCM_INSERTITEMW, i, ctypes.addressof(item))

    def drag_proc(h, m, w, lp):
        if m in (0x0202, 0x0215):  # WM_LBUTTONUP・WM_CAPTURECHANGED: 引きずり終わり。外へ引き出したことにする
            if m == 0x0202:
                u.ReleaseCapture()
            u.CreateWindowExW(0, "EditTestFind", f"edit-testapp {tag} torn", WS_OVERLAPPEDWINDOW | WS_VISIBLE,
                              300, 300, 300, 200, None, None, inst, None)
            return 0
        return u.DefWindowProcW(h, m, w, lp)

    drag_wndproc = WNDPROC(drag_proc)
    u.RegisterClassW(ctypes.byref(WNDCLASSW(lpfnWndProc=drag_wndproc, hInstance=inst, lpszClassName="EditTestDrag")))
    drag = u.CreateWindowExW(0, "EditTestDrag", "", WS_POPUP, 0, 0, 0, 0, None, None, inst, None)
    old = [0]

    def tab_proc(h, m, w, lp):
        r = u.CallWindowProcW(old[0], h, m, w, lp)
        if m == WM_LBUTTONDOWN:
            u.SetCapture(drag)  # 選んだあと、引きずる用意 (Delphi の BeginDrag と同じく、隠れた窓がつかむ)
        return r

    tab_wndproc = WNDPROC(tab_proc)
    old[0] = u.SetWindowLongPtrW(tabs, -4, ctypes.cast(tab_wndproc, ctypes.c_void_p))  # GWLP_WNDPROC
    return tabs, [drag_wndproc, tab_wndproc]


def main() -> None:
    tag = sys.argv[1] if len(sys.argv) > 1 else "x"
    inst = k.GetModuleHandleW(None)
    edit = [0]
    tabs = [0]
    menu = make_menu()

    def proc(hwnd, msg, wp, lp):
        if msg == WM_SIZE and edit[0]:
            w, h = lp & 0xFFFF, lp >> 16 & 0xFFFF
            bottom = TAB_H if tabs[0] else 0
            u.MoveWindow(edit[0], 0, BAR_H, w, h - BAR_H - bottom, True)
            if tabs[0]:
                u.MoveWindow(tabs[0], 0, h - TAB_H, w, TAB_H, True)
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
            if cmd == ID_FIND:
                open_find(hwnd, inst)
            else:
                u.SetFocus(edit[0])
            return 0
        if msg == WM_COMMAND and wp & 0xFFFF == ID_SAVE:
            text = ctypes.create_unicode_buffer("[S]")
            u.SendMessageW(edit[0], EM_REPLACESEL, 1, ctypes.addressof(text))
            return 0
        if msg == WM_DESTROY:
            u.PostQuitMessage(0)
            return 0
        return u.DefWindowProcW(hwnd, msg, wp, lp)

    wndproc = WNDPROC(proc)
    wc = WNDCLASSW(lpfnWndProc=wndproc, hInstance=inst, hbrBackground=6, lpszClassName="EditTestApp")  # COLOR_WINDOW+1
    u.RegisterClassW(ctypes.byref(wc))
    find_proc = WNDPROC(lambda h, m, w, lp: u.DefWindowProcW(h, m, w, lp))
    u.RegisterClassW(ctypes.byref(WNDCLASSW(lpfnWndProc=find_proc, hInstance=inst, hbrBackground=16,  # COLOR_BTNFACE+1
                                            lpszClassName="EditTestFind")))
    top =u.CreateWindowExW(0, "EditTestApp", f"edit-testapp {tag}", WS_OVERLAPPEDWINDOW | WS_VISIBLE,
                            200, 200, 500, 400, None, None, inst, None)
    edit[0] = u.CreateWindowExW(0, "EDIT", "", WS_CHILD | WS_VISIBLE | WS_VSCROLL | ES_MULTILINE | ES_AUTOVSCROLL
                                | ES_WANTRETURN, 0, BAR_H, 480, 330, top, None, inst, None)
    keep = []
    if os.environ.get("EDIT_TESTAPP_TABS"):
        tabs[0], keep = make_tabs(top, inst, tag)
        r = wintypes.RECT()
        u.GetClientRect(top, ctypes.byref(r))
        proc(top, WM_SIZE, 0, r.bottom << 16 | r.right)
    u.SetFocus(edit[0])
    accel = u.CreateAcceleratorTableW(ctypes.byref(ACCEL(FVIRTKEY | FCONTROL, ord("S"), ID_SAVE)), 1)
    msg = wintypes.MSG()
    while u.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
        if u.TranslateAcceleratorW(top, accel, ctypes.byref(msg)):
            continue
        u.TranslateMessage(ctypes.byref(msg))
        u.DispatchMessageW(ctypes.byref(msg))


if __name__ == "__main__":
    main()
