"""ToolDock の自動確認。tooldock.py --selftest <ツール名...> から呼ばれる。

実際にツールを起動して取り込み、Win32 から見た状態 (枠・タスクバー・位置・表示) を確かめる。
ドックの範囲だけを docs/ に撮る。最後に取り込んだツールは閉じる (このテストが起動したものだけ)。
    --selftest crash <ツール名>   取り込んだまま異常終了する (そのあと --rescue で戻るかを見る)
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from PIL import ImageGrab

import tooldock as td

DOCS = Path(__file__).with_name("docs")
results: list[tuple[bool, str]] = []


def check(ok: bool, what: str) -> None:
    results.append((bool(ok), what))
    print(("PASS " if ok else "FAIL ") + what, flush=True)


def visible(hwnd: int) -> bool:
    return bool(td.user32.IsWindowVisible(hwnd))


def on_top(dock: td.Dock, hwnd: int) -> bool:
    """ドックの本体の中央で、いちばん手前にある窓が hwnd か (位置と表示状態だけでは、後ろに回っていても通ってしまう)。"""
    x, y, w, h = dock.body_rect()
    at = td.user32.WindowFromPoint(td.wintypes.POINT(x + w // 2, y + h // 2))
    return td.user32.GetAncestor(at, td.GA_ROOT) == hwnd


def near(a, b, tol=2) -> bool:
    return all(abs(x - y) <= tol for x, y in zip(a, b))


def shot(dock: td.Dock, name: str) -> None:
    DOCS.mkdir(exist_ok=True)
    x, y, w, h = td.rect_of(dock.hwnd)
    ImageGrab.grab(bbox=(x, y, x + w, y + h), all_screens=True).save(DOCS / name)


def run(dock: td.Dock, args: list[str]) -> None:
    crash = args[:1] == ["crash"]
    names = args[1:] if crash else args
    tools = [t for t in dock.tools if t["name"] in names]
    steps = (crash_steps if crash else steps_all)(dock, tools)

    def tick():
        try:
            delay = next(steps)
        except StopIteration:
            return
        dock.root.after(delay, tick)

    dock.root.after(300, tick)


def crash_steps(dock: td.Dock, tools: list[dict]):
    dock.launch(tools[0])
    yield 800
    t = dock.tabs[0]
    check(td.STATE.exists(), f"取り込んだ窓を state.json に控えた (hwnd={t.hwnd})")
    print(f"HWND {t.hwnd}", flush=True)
    os._exit(3)  # 後片付けをせずに落ちる
    yield 0


def steps_all(dock: td.Dock, tools: list[dict]):
    for tool in tools:
        dock.launch(tool)
        yield 800
    check(len(dock.tabs) == len(tools), f"{len(tools)} 個のツールをタブに取り込んだ ({[t.name for t in dock.tabs]})")
    for t in dock.tabs:
        style = td.user32.GetWindowLongPtrW(t.hwnd, td.GWL_STYLE)
        ex = td.user32.GetWindowLongPtrW(t.hwnd, td.GWL_EXSTYLE)
        check(style & td.WS_CAPTION != td.WS_CAPTION, f"{t.name}: タイトルバーを外した")
        check(ex & td.WS_EX_TOOLWINDOW and not ex & td.WS_EX_APPWINDOW, f"{t.name}: タスクバーのボタンを消す設定にした")

    a, b = dock.tabs[-1], dock.tabs[0]
    check(dock.active is a and visible(a.hwnd) and not visible(b.hwnd), "選んだタブだけが見え、他は隠れている")
    check(near(td.rect_of(a.hwnd), dock.body_rect()), f"選んだ窓がドックの本体に重なった {td.rect_of(a.hwnd)} ~ {dock.body_rect()}")
    check(on_top(dock, a.hwnd), "選んだ窓がドックより手前に見えている")
    shot(dock, "selftest-1.png")

    dock.select(b)
    yield 500
    check(visible(b.hwnd) and not visible(a.hwnd), "タブを切り替えると窓が入れ替わる")
    check(near(td.rect_of(b.hwnd), dock.body_rect()), "切り替えた窓も本体に重なった")
    check(on_top(dock, b.hwnd), "切り替えた窓もドックより手前に見えている")
    shot(dock, "selftest-2.png")

    dock.root.geometry("900x600+300+200")
    yield 600
    check(near(td.rect_of(b.hwnd), dock.body_rect()), f"ドックを動かす・大きさを変えると窓がついてくる {td.rect_of(b.hwnd)}")
    check(on_top(dock, b.hwnd), "動かしたあとも手前に見えている")

    dock.root.iconify()
    yield 600
    check(not visible(b.hwnd), "ドックを最小化すると窓も隠れる")
    dock.root.deiconify()
    yield 800
    check(visible(b.hwnd) and near(td.rect_of(b.hwnd), dock.body_rect()), "ドックを戻すと窓も戻る")
    check(on_top(dock, b.hwnd), "戻したあとも手前に見えている")

    # 外す: 元の枠・タスクバー・位置に戻る
    dock.release(b)
    yield 500
    style = td.user32.GetWindowLongPtrW(b.hwnd, td.GWL_STYLE)
    ex = td.user32.GetWindowLongPtrW(b.hwnd, td.GWL_EXSTYLE)
    check(style == b.style and ex == b.exstyle and visible(b.hwnd), f"{b.name}: 外すと元の枠・タスクバーの設定に戻り、見える")
    check(dock.active is a and visible(a.hwnd), "外したあと、残ったタブが表に出る")

    # ツールを閉じる: タブが消える
    dock.close_tab(a)
    for _ in range(20):
        yield 300
        if not dock.tabs:
            break
    check(not dock.tabs, f"{a.name} を閉じるとタブが消える")

    td.user32.PostMessageW(b.hwnd, td.WM_CLOSE, 0, 0)  # テストで起動したものを片付ける
    yield 500
    dock.quit()
    check(not td.STATE.exists(), "ドックを閉じると控え (state.json) も消える")
    failed = [w for ok, w in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} PASS", flush=True)
    if failed:
        sys.exit(1)
