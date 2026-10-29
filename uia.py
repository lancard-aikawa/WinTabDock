"""UI Automation で、窓の中のボタンなどを押す (標準ライブラリの ctypes だけで COM を呼ぶ)。

エクスプローラーの上の部分 (タブ・戻る・アドレス・「新規作成」「表示」「…」) とそのメニューは、Windows App SDK の
窓 (Microsoft.UI.Content.*SiteBridge) が描いていて、本物のマウスの入力しか受けない。窓へ送った「押した」の
メッセージは捨てられ、スマホから押しても何も起きなかった (2026-10-30)。手元のマウスは動かさないので、
画面の読み上げと同じ UI Automation で、押した所にある部品を押す。
"""

import ctypes
import threading
from ctypes import wintypes

# 押してもメッセージでは届かない窓の種類
BRIDGE_CLASSES = {
    "Microsoft.UI.Content.DesktopChildSiteBridge",  # エクスプローラーの上の部分 (Windows App SDK)
    "Microsoft.UI.Content.PopupWindowSiteBridge",  # そのメニュー
    "Windows.UI.Composition.DesktopWindowContentBridge",  # Windows の XAML Islands (メモ帳など)
    "Xaml_WindowedPopupClass",  # そのメニュー
}

_ole32 = ctypes.WinDLL("ole32")
_oleaut32 = ctypes.WinDLL("oleaut32")
_oleaut32.SysFreeString.argtypes = [ctypes.c_void_p]
_ole32.CoCreateInstance.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p,
                                    ctypes.POINTER(ctypes.c_void_p)]


class _GUID(ctypes.Structure):
    _fields_ = [("d1", ctypes.c_uint32), ("d2", ctypes.c_uint16), ("d3", ctypes.c_uint16), ("d4", ctypes.c_ubyte * 8)]


def _guid(s: str) -> _GUID:
    g = _GUID()
    _ole32.CLSIDFromString(ctypes.c_wchar_p(s), ctypes.byref(g))
    return g


_CLSID_CUIAutomation = _guid("{FF48DBA4-60EF-4201-AA87-54103EEF594E}")
_IID_IUIAutomation = _guid("{30CBE57D-D9D0-452A-AB13-7AC5AC4825EE}")
# 押し方 (パターンの番号, インターフェイス, 名前)。先にあるものほど優先する。
# 項目を開くもの (「表示」) は Invoke と ExpandCollapse の両方を持ち、Invoke で開いた
_PATTERNS = [
    (10000, _guid("{FB377FBE-8EA6-46D5-9C73-6499642D3059}"), "invoke"),  # ボタン・メニューの項目
    (10015, _guid("{94CF8058-9B8D-4AB9-8BFD-4CD0A33C8C70}"), "toggle"),  # チェック・「大アイコン」など
    (10010, _guid("{A8EFA66A-0FDA-421A-9194-38021F3578EA}"), "select"),  # タブ・一覧の項目
    (10005, _guid("{619BE086-1F4E-4EE4-BAFA-210128738730}"), "expand"),  # ドロップダウン
]
_TREE_SCOPE_DESCENDANTS = 4
_PP = ctypes.POINTER(ctypes.c_void_p)

# vtable の番号 (UIAutomationClient.h の並び。IUnknown の 3 つを含む)
_UIA_ELEMENT_FROM_HANDLE, _UIA_CREATE_TRUE_CONDITION = 6, 21
_EL_SET_FOCUS, _EL_FIND_ALL, _EL_GET_PATTERN_AS = 3, 6, 14
_EL_CONTROL_TYPE, _EL_NAME, _EL_FOCUSABLE, _EL_RECT = 21, 23, 27, 43
_TOOLTIP = 50022  # UIA_ToolTipControlTypeId
_ARR_LENGTH, _ARR_GET = 3, 4


def _call(obj, index: int, argtypes: list, *args) -> int:
    vtbl = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    return ctypes.WINFUNCTYPE(ctypes.HRESULT, ctypes.c_void_p, *argtypes)(vtbl[index])(obj, *args)


def _release(obj) -> None:
    if obj:
        vtbl = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(vtbl[2])(obj)


_local = threading.local()


def _automation() -> ctypes.c_void_p:
    """このスレッドの IUIAutomation (COM はスレッドごとに初期化する)。"""
    uia = getattr(_local, "uia", None)
    if uia is None:
        _ole32.CoInitializeEx(None, 0)  # COINIT_MULTITHREADED (UI Automation の使い手に勧められている)
        uia = ctypes.c_void_p()
        _ole32.CoCreateInstance(ctypes.byref(_CLSID_CUIAutomation), None, 1, ctypes.byref(_IID_IUIAutomation),
                                ctypes.byref(uia))
        _local.uia = uia
    return uia


def _name(el) -> str:
    b = ctypes.c_void_p()
    try:
        _call(el, _EL_NAME, [_PP], ctypes.byref(b))
    except OSError:
        return ""
    s = ctypes.wstring_at(b.value) if b.value else ""
    _oleaut32.SysFreeString(b)
    return s


def _rect(el) -> tuple[int, int, int, int]:
    r = wintypes.RECT()
    _call(el, _EL_RECT, [ctypes.POINTER(wintypes.RECT)], ctypes.byref(r))
    return r.left, r.top, r.right, r.bottom


def _pattern(el, pid: int, iid: _GUID):
    p = ctypes.c_void_p()
    try:
        _call(el, _EL_GET_PATTERN_AS, [ctypes.c_int, ctypes.c_void_p, _PP], pid, ctypes.byref(iid), ctypes.byref(p))
    except OSError:
        return None
    return p if p.value else None


def _elements(hwnd: int):
    """hwnd の中の部品をすべて (IUIAutomationElementArray)。使い終わったら _release する。"""
    uia = _automation()
    if not uia:
        raise OSError("UI Automation を作れなかった")
    root, cond, arr = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_void_p()
    try:
        _call(uia, _UIA_ELEMENT_FROM_HANDLE, [wintypes.HWND, _PP], hwnd, ctypes.byref(root))
        _call(uia, _UIA_CREATE_TRUE_CONDITION, [_PP], ctypes.byref(cond))
        _call(root, _EL_FIND_ALL, [ctypes.c_int, ctypes.c_void_p, _PP], _TREE_SCOPE_DESCENDANTS, cond, ctypes.byref(arr))
    finally:
        _release(cond)
        _release(root)
    return arr


_tooltips: dict[int, bool] = {}
_tooltips_lock = threading.Lock()


def is_tooltip(hwnd: int) -> bool:
    """hwnd (BRIDGE_CLASSES の小窓) が説明の吹き出しか。メニューと窓の種類もスタイルも同じで、中身で見分ける。
    UI Automation で押すと部品にキーボードのフォーカスが移り、吹き出しが出たまま (PC ではマウスが離れると消える)
    アドレスの欄に重なった。窓ごとに一度だけ調べて覚える (画面を送るたびに呼ばれる)"""
    with _tooltips_lock:
        if hwnd in _tooltips:
            return _tooltips[hwnd]
    found = False
    arr = ctypes.c_void_p()
    try:
        arr = _elements(hwnd)
        n = ctypes.c_int()
        _call(arr, _ARR_LENGTH, [ctypes.POINTER(ctypes.c_int)], ctypes.byref(n))
        for i in range(n.value):
            el, kind = ctypes.c_void_p(), ctypes.c_int()
            _call(arr, _ARR_GET, [ctypes.c_int, _PP], i, ctypes.byref(el))
            _call(el, _EL_CONTROL_TYPE, [ctypes.POINTER(ctypes.c_int)], ctypes.byref(kind))
            _release(el)
            if kind.value == _TOOLTIP:
                found = True
                break
    except OSError:
        pass
    finally:
        _release(arr)
    with _tooltips_lock:
        if len(_tooltips) > 200:  # 閉じた窓の分を捨てる
            for h in [h for h in _tooltips if not ctypes.windll.user32.IsWindow(h)]:
                del _tooltips[h]
        _tooltips[hwnd] = found
    return found


def press_at(hwnd: int, x: int, y: int) -> str | None:
    """窓 hwnd の中で、画面の座標 (x, y) にある部品を押す。押せる部品のうち一番小さいものを、Invoke・Toggle・
    選ぶ・開く の順に試す。押せる部品が無ければ、文字の欄などにフォーカスを移す。何をしたか (記録用) を返す。"""
    arr = ctypes.c_void_p()
    held = []
    try:
        arr = _elements(hwnd)
        n = ctypes.c_int()
        _call(arr, _ARR_LENGTH, [ctypes.POINTER(ctypes.c_int)], ctypes.byref(n))
        best = None  # (面積, 部品, パターン, 名前)
        focus = None  # (面積, 部品)
        for i in range(n.value):
            el = ctypes.c_void_p()
            _call(arr, _ARR_GET, [ctypes.c_int, _PP], i, ctypes.byref(el))
            held.append(el)
            left, top, right, bottom = _rect(el)
            if not (left <= x < right and top <= y < bottom):
                continue
            area = (right - left) * (bottom - top)
            for pid, iid, kind in _PATTERNS:
                if best and area >= best[0]:
                    break
                p = _pattern(el, pid, iid)
                if p:
                    if best:
                        _release(best[2][0])
                    best = (area, el, (p, kind))
                    break
            can_focus = wintypes.BOOL()
            _call(el, _EL_FOCUSABLE, [ctypes.POINTER(wintypes.BOOL)], ctypes.byref(can_focus))
            if can_focus.value and (not focus or area < focus[0]):
                focus = (area, el)
        if best and (not focus or best[0] <= focus[0]):
            _, el, (p, kind) = best
            done = f"{kind} {_name(el)!r}"  # 押す前に読む (「…」は開くと名前がコマンドバーの名前に変わった)
            try:
                _call(p, 3, [])  # Invoke / Toggle / Select / Expand は、どれも最初の手続き
            finally:
                _release(p)
            return done
        if best:
            _release(best[2][0])
        if focus:
            _call(focus[1], _EL_SET_FOCUS, [])
            return f"focus {_name(focus[1])!r}"
        return None
    except OSError as e:  # 相手が閉じた・応えない
        return f"failed {e}"
    finally:
        for el in held:
            _release(el)
        _release(arr)
