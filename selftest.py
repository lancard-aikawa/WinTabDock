"""ToolDock の自動確認。tooldock.py --selftest <ツール名...> から呼ばれる。

実際にツールを起動して取り込み、Win32 から見た状態 (枠・タスクバー・位置・表示) を確かめる。
ドックの範囲だけを docs/ に撮る。最後に取り込んだツールは閉じる (このテストが起動したものだけ)。
    --selftest crash <ツール名>   取り込んだまま異常終了する (そのあと --rescue で戻るかを見る)
"""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from PIL import ImageGrab

import tooldock as td

DOCS = Path(__file__).with_name("docs")
# 「別のアプリ」の役。ドックの左上に重なる位置に出す
OTHER_APP = ("import tkinter as t; r=t.Tk(); r.title('other app'); r.geometry('800x500+0+0'); "
             "t.Label(r, text='other app', bg='#2d6a4f', fg='white').pack(fill='both', expand=True); r.mainloop()")
results: list[tuple[bool, str]] = []


def check(ok: bool, what: str) -> None:
    results.append((bool(ok), what))
    print(("PASS " if ok else "FAIL ") + what, flush=True)


def visible(hwnd: int) -> bool:
    return bool(td.user32.IsWindowVisible(hwnd))


def top_at(x: int, y: int) -> int:
    return td.user32.GetAncestor(td.user32.WindowFromPoint(td.wintypes.POINT(x, y)), td.GA_ROOT)


def on_top(dock: td.Dock, hwnd: int) -> bool:
    """ドックの本体の中央で、いちばん手前にある窓が hwnd か (位置と表示状態だけでは、後ろに回っていても通ってしまう)。"""
    x, y, w, h = dock.body_rect()
    return top_at(x + w // 2, y + h // 2) == hwnd


def count_processes(exe: str) -> int:
    return sum(1 for pid in td.all_pids() if os.path.normcase(td.process_path(pid)) == os.path.normcase(exe))


def drag(x0: int, y0: int, x1: int, y1: int, probe=None) -> None:
    """本物のマウスでドラッグする。別スレッドから呼ぶ (枠のドラッグ中は Windows のサイズ変更ループが Tk を止める)。
    probe はドラッグの途中 (ボタンを押したまま) で呼ぶ。"""
    td.user32.SetCursorPos(x0, y0)
    time.sleep(0.2)
    td.user32.mouse_event(0x0002, 0, 0, 0, 0)
    for i in range(1, 11):
        time.sleep(0.03)
        td.user32.SetCursorPos(x0 + (x1 - x0) * i // 10, y0 + (y1 - y0) * i // 10)
        if i == 5 and probe:
            time.sleep(0.3)
            probe()
    time.sleep(0.2)
    td.user32.mouse_event(0x0004, 0, 0, 0, 0)


def tool_visible_now(dock_hwnd: int, tool_hwnd: int, out: list) -> None:
    """別スレッドから: ドックの見えている枠の中ほど (本体) で、いちばん手前がツールか。Tk は触らない。"""
    x, y, w, h = frame_rect(dock_hwnd)
    out.append(top_at(x + w // 2, y + h * 2 // 3) == tool_hwnd)


def frame_rect(hwnd: int) -> tuple[int, int, int, int]:
    """見えている枠 (Windows 10/11 は GetWindowRect が見えない縁を含む)。"""
    r = td.wintypes.RECT()
    ctypes.windll.dwmapi.DwmGetWindowAttribute(td.wintypes.HWND(hwnd), 9, ctypes.byref(r), ctypes.sizeof(r))
    return r.left, r.top, r.right - r.left, r.bottom - r.top


def click(x: int, y: int) -> None:
    """本物のマウス操作でクリックする。プログラムからの前面化と、人のクリックとでは Windows の扱いが違うため。"""
    td.user32.SetCursorPos(x, y)
    td.user32.mouse_event(0x0002, 0, 0, 0, 0)  # LEFTDOWN
    td.user32.mouse_event(0x0004, 0, 0, 0, 0)  # LEFTUP


def near(a, b, tol=2) -> bool:
    return all(abs(x - y) <= tol for x, y in zip(a, b))


def shot(dock: td.Dock, name: str) -> None:
    DOCS.mkdir(exist_ok=True)
    x, y, w, h = td.rect_of(dock.hwnd)
    ImageGrab.grab(bbox=(x, y, x + w, y + h), all_screens=True).save(DOCS / name)


def guard(tools: list[dict]) -> str | None:
    """試験を始めてよいか。人が使っているツールやドックに手を出さないため。

    試験の「起動」は起動中の窓があれば取り込み、最後に閉じる。対象のツールを人が使っていたとき、
    それを取り込んで閉じてしまった (RepoTether と PortWho)。
    """
    running = [t["name"] for t in tools if count_processes(t["exe"])]
    if running:
        return f"{', '.join(running)} が起動中です。閉じてから流してください (試験はそれを取り込んで最後に閉じる)"
    if td.other_docks():
        return "ほかの ToolDock が開いています。閉じてから流してください"
    return None


MODES = ("crash", "session-save", "session-restore")


def run(dock: td.Dock, args: list[str]) -> None:
    mode = args[0] if args[:1] and args[0] in MODES else None
    tools = dock.tools  # main が試験の対象だけに絞ってある
    # session-restore は、直前の session-save が起動して残したツールを取り込むのが試験の中身なので、起動中でよい
    reason = guard(tools) if mode != "session-restore" else (
        "ほかの ToolDock が開いています。閉じてから流してください" if td.other_docks() else None)
    if reason:
        print(f"中止: {reason}", flush=True)
        os._exit(2)  # まだ何も取り込んでいない
    # ドックが前回のタブを開き直す (300ms 後) 前に数える
    dock.process_counts_before = {t["name"]: count_processes(t["exe"]) for t in tools}
    steps = {"crash": crash_steps, "session-save": session_save_steps,
             "session-restore": session_restore_steps}.get(mode, steps_all)(dock, tools)

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
    print(f"PID {os.getpid()}", flush=True)
    os._exit(3)  # 後片付けをせずに落ちる
    yield 0


SESSION_GEOMETRY = "900x600+300+200"


def session_save_steps(dock: td.Dock, tools: list[dict]):
    """ツールを取り込み、大きさ・位置を変えてドックを閉じる。ツールは元の窓に戻って動き続ける。"""
    for tool in tools:
        dock.launch(tool)
        yield 800
    dock.root.geometry(SESSION_GEOMETRY)
    yield 600
    names = [t.name for t in dock.tabs]
    dock.quit()
    saved = td.json.loads(dock.session_path.read_text(encoding="utf-8"))
    check(saved.get("tabs") == names and saved.get("geometry", "").startswith(SESSION_GEOMETRY),
          f"閉じたときのタブと大きさ・位置を覚えた {saved}")
    print(f"\n{sum(ok for ok, _ in results)}/{len(results)} PASS", flush=True)


def session_restore_steps(dock: td.Dock, tools: list[dict]):
    """前回のタブを開き直す (起動中なので取り込む)。最後にツールを閉じる。"""
    for _ in range(30):
        yield 300
        if len(dock.tabs) == len(tools):
            break
    expected = [t["name"] for t in tools]  # 試験の前回のタブは session-save が全部を開いて閉じたもの
    check([t.name for t in dock.tabs] == expected, f"前回のタブを開き直した ({[t.name for t in dock.tabs]})")
    check(dock.root.geometry().startswith(SESSION_GEOMETRY), f"前回の大きさ・位置に戻った ({dock.root.geometry()})")
    before, after = dock.process_counts_before, {t["name"]: count_processes(t["exe"]) for t in tools}
    # 起動中だったものは取り込むだけ (数が変わらない)、起動していなかったものは起動する
    check(all(after[n] == before[n] if before[n] else after[n] > 0 for n in before),
          f"起動中のものは取り込み、なかったものは起動した ({before} → {after})")
    if dock.tabs:
        yield 800
        check(on_top(dock, dock.active.hwnd), "開き直したタブの窓が手前に見えている")
    for t in list(dock.tabs):
        dock.close_tab(t)
    for _ in range(20):
        yield 300
        if not dock.tabs:
            break
    dock.quit()
    failed = [w for ok, w in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} PASS", flush=True)
    if failed:
        sys.exit(1)


def steps_all(dock: td.Dock, tools: list[dict]):
    for tool in tools:
        dock.launch(tool)
        yield 800
    check(len(dock.tabs) == len(tools), f"{len(tools)} 個のツールをタブに取り込んだ ({[t.name for t in dock.tabs]})")

    # 取り込み済みのツールをもう一度「起動」しても、2 つ目を起動せずにそのタブへ切り替える
    exe = tools[0]["exe"]
    before = count_processes(exe)
    dock.launch(tools[0])
    yield 1500
    check(count_processes(exe) == before and len(dock.tabs) == len(tools),
          f"取り込み済みの {tools[0]['name']} をもう一度起動しても、2 つ目は起動しない (プロセス {before} → {count_processes(exe)})")
    check(dock.active and dock.active.name == tools[0]["name"], "そのタブに切り替わる")

    # メニュー: ふだんの項目 (起動 / 切り替え) が上、「もう 1 つ起動」は一番下のサブメニューだけ
    m = dock.build_menu()
    labels = [m.entrycget(i, "label") if m.type(i) != "separator" else "---" for i in range(m.index("end") + 1)]
    check(labels[-1] == "もう 1 つ起動" and not any("もう 1 つ" in s for s in labels[:-1]),
          f"「もう 1 つ起動」はふだんの項目と分けて一番下にある {labels}")
    check(f"{tools[0]['name']} に切り替え" in labels, "取り込み済みのツールは「切り替え」になっている")
    last = dock.tabs[-1].button  # 以降は最後のタブが選ばれている前提。本物のクリックで選び直す
    click(last.winfo_rootx() + last.winfo_width() // 2, last.winfo_rooty() + last.winfo_height() // 2)
    yield 700

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

    # タブは本物のクリックで切り替える。dock.select() をプログラムから呼ぶと、前面が別プロセス (前のタブ) のため
    # SetForegroundWindow が拒否され、実際の操作では起きない失敗になる
    click(b.button.winfo_rootx() + b.button.winfo_width() // 2, b.button.winfo_rooty() + b.button.winfo_height() // 2)
    yield 700
    check(visible(b.hwnd) and not visible(a.hwnd), "タブを切り替えると窓が入れ替わる")
    check(near(td.rect_of(b.hwnd), dock.body_rect()), "切り替えた窓も本体に重なった")
    check(on_top(dock, b.hwnd), "切り替えた窓もドックより手前に見えている")
    shot(dock, "selftest-2.png")

    # ドックの右下の角を本物のマウスでドラッグして大きさを変える
    fx, fy, fw, fh = frame_rect(dock.hwnd)
    if os.environ.get("TOOLDOCK_DEBUG"):
        names = {dock.hwnd: "dock", a.hwnd: a.name, b.hwnd: b.name}
        for label, (px, py) in {"外側 +3": (fx + fw + 3, fy + fh + 3), "内側 -2": (fx + fw - 2, fy + fh - 2)}.items():
            at = top_at(px, py)
            ht = td.user32.SendMessageW(dock.hwnd, 0x0084, 0, (py << 16) | (px & 0xFFFF))  # WM_NCHITTEST
            print(f"  debug 角の{label} ({px},{py}): 窓={names.get(at, at)} / ドックの判定={ht} (17=右下の角)", flush=True)
        print(f"  debug 見えている枠={frame_rect(dock.hwnd)} GetWindowRect={td.rect_of(dock.hwnd)}"
              f" 本体={dock.body_rect()} ツール={td.rect_of(b.hwnd)}", flush=True)
    t = threading.Thread(target=drag, args=(fx + fw + 3, fy + fh + 3, fx + fw - 150, fy + fh - 100))
    t.start()
    while t.is_alive():
        yield 100
    yield 600
    nx, ny, nw, nh = frame_rect(dock.hwnd)
    check(nw < fw - 100 and nh < fh - 60, f"ドックの右下の角をドラッグすると大きさが変わる ({fw}x{fh} → {nw}x{nh})")
    check(near(td.rect_of(b.hwnd), dock.body_rect()) and on_top(dock, b.hwnd), "ツールの窓も新しい大きさについてくる")

    # タイトルバーを本物のマウスでドラッグして動かす
    fx, fy, fw, fh = frame_rect(dock.hwnd)
    during: list[bool] = []
    t = threading.Thread(target=drag, args=(fx + fw // 2, fy + 12, fx + fw // 2 + 120, fy + 12 + 80,
                                            lambda: tool_visible_now(dock.hwnd, b.hwnd, during)))
    t.start()
    while t.is_alive():
        yield 100
    yield 600
    nx, ny, _, _ = frame_rect(dock.hwnd)
    check(abs(nx - fx - 120) <= 4 and abs(ny - fy - 80) <= 4, f"タイトルバーをドラッグするとドックが動く ({fx},{fy} → {nx},{ny})")
    check(during == [True], "ドラッグしている間もツールが見えている (本体が真っ暗にならない)")
    check(near(td.rect_of(b.hwnd), dock.body_rect()) and on_top(dock, b.hwnd), "ツールの窓も動いた先についてくる")

    # 「+ 起動 / 取り込む」を押している間と、開いたメニューを閉じたあとも、ツールが見えている
    btn = dock.menu_button
    bx0, by0 = btn.winfo_rootx() + btn.winfo_width() // 2, btn.winfo_rooty() + btn.winfo_height() // 2
    pressed: list[bool] = []

    def press_menu_button():
        td.user32.SetCursorPos(bx0, by0)
        time.sleep(0.2)
        td.user32.mouse_event(0x0002, 0, 0, 0, 0)
        time.sleep(0.4)
        tool_visible_now(dock.hwnd, b.hwnd, pressed)
        td.user32.mouse_event(0x0004, 0, 0, 0, 0)
        time.sleep(0.8)
        tool_visible_now(dock.hwnd, b.hwnd, pressed)  # メニューが開いている間
        td.user32.keybd_event(0x1B, 0, 0, 0)  # Esc でメニューを閉じる
        td.user32.keybd_event(0x1B, 0, 2, 0)

    t = threading.Thread(target=press_menu_button)
    t.start()
    while t.is_alive():
        yield 100
    yield 500
    check(pressed == [True, True], f"「+ 起動 / 取り込む」を押している間もメニューを開いている間もツールが見えている ({pressed})")
    check(on_top(dock, b.hwnd), "メニューを閉じたあともツールが見えている")

    dock.root.geometry("900x600+300+200")
    yield 600
    check(near(td.rect_of(b.hwnd), dock.body_rect()), f"ドックを動かす・大きさを変えると窓がついてくる {td.rect_of(b.hwnd)}")
    check(on_top(dock, b.hwnd), "動かしたあとも手前に見えている")

    # 別のアプリの窓がドックの左上に重なっている場面 (ドックは 300,200 900x600、別の窓は 0,0 800x500)
    other_proc = subprocess.Popen([sys.executable, "-c", OTHER_APP])
    other = None
    for _ in range(30):
        yield 200
        other = td.find_window(td.process_tree(other_proc.pid))
        if other:
            break
    yield 800
    dx, dy, dw, dh = td.rect_of(dock.hwnd)
    bx, by, bw, bh = dock.body_rect()
    covered_body = (bx + 100, by + 50)        # 別の窓が重なっている、ドック本体の点
    covered_bar = (dx + 30, dy + 45)          # 別の窓が重なっている、タブの帯の点
    free_title = (dx + dw - 200, dy + 12)     # 別の窓が重なっていない、ドックのタイトルバー
    free_tool = (dx + dw - 100, by + bh // 2)  # 別の窓が重なっていない、ツールの窓の点
    check(other and top_at(*covered_body) == other, "別のアプリの窓が前に出て、ドックの一部を隠している")

    click(*free_title)  # A: ドックのタイトルバーをクリックして戻る
    yield 700
    check(top_at(*covered_body) == b.hwnd, "A: ドックのタイトルバーをクリックすると、ツールの窓も前に戻る")
    check(top_at(*covered_bar) == dock.hwnd, "A: タブの帯も前に戻る")

    click(20, 20)  # 別のアプリを前に出す
    yield 700
    click(*free_tool)  # B: 見えているツールの部分をクリックして戻る
    yield 700
    check(top_at(*covered_body) == b.hwnd, "B: ツールをクリックすると、ツールの窓全体が前に出る")
    check(top_at(*covered_bar) == dock.hwnd, "B: タブの帯もツールと一緒に前に出る")
    td.user32.PostMessageW(other, td.WM_CLOSE, 0, 0)
    yield 500

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

    # 自動取り込み: 外した窓はすぐに取り込み直さない / ドックの外で起動したツールはタブになる
    yield 1500
    check(not any(t.hwnd == b.hwnd for t in dock.tabs) and visible(b.hwnd), f"外した {b.name} は自動で取り込み直さない")
    tool_a = next(t for t in tools if t["name"] == a.name)
    outside = subprocess.Popen([tool_a["exe"]], cwd=str(Path(tool_a["exe"]).parent))  # ドックを通さずに起動
    for _ in range(40):
        yield 300
        if any(t.name == a.name for t in dock.tabs):
            break
    tab = next((t for t in dock.tabs if t.name == a.name), None)
    check(tab is not None and tab.pid in td.process_tree(outside.pid),
          f"ドックの外で起動した {a.name} も自動でタブになる")
    if tab:
        yield 800
        check(on_top(dock, tab.hwnd), "自動で取り込んだ窓も手前に見えている")
        dock.close_tab(tab)
        for _ in range(20):
            yield 300
            if not dock.tabs:
                break

    td.user32.PostMessageW(b.hwnd, td.WM_CLOSE, 0, 0)  # テストで起動したものを片付ける
    yield 500
    dock.quit()
    check(not td.STATE.exists(), "ドックを閉じると控え (state.json) も消える")
    failed = [w for ok, w in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} PASS", flush=True)
    if failed:
        sys.exit(1)
