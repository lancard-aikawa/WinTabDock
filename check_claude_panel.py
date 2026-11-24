"""Claude の欄の、VS Code の窓の自動取り込みを確かめる。  uv run check_claude_panel.py [--here]

ふだんは人の画面に出ない別のデスクトップで流す。--here を付けると人の画面で流す (窓が見える)。

Auto-adopt check. A small Tk window titled like VS Code stands in for VS Code
(a real isolated VS Code does not open a window while the user's VS Code is updating).
The dock reads a fake ~/.claude that lists only this window, so the user's VS Code is never touched."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent / "experiments"))
import hidden_desktop  # noqa: E402

if "--here" not in sys.argv and not hidden_desktop.inside():
    # 試験の窓とドックを人の画面に出さない (前面を取ると、人が打っている文字が途切れる)。check_remote と同じ仕組み
    print(f"人の画面に出ない別のデスクトップ ({hidden_desktop.NAME}) で流す (人の画面で流すなら --here)", flush=True)
    sys.exit(hidden_desktop.rerun_inside())

SP =Path(tempfile.mkdtemp(prefix="wtd-claude-"))
(SP / "home").mkdir(parents=True)
os.environ["WINTABDOCK_HOME"] = str(SP / "home")

import claude_sessions as cs  # noqa: E402
import claude_panel  # noqa: E402
import wintabdock as w  # noqa: E402

folder = SP / "wtdprobe"
folder.mkdir()
fake = SP / "fakeclaude"
(fake / "ide").mkdir(parents=True)
(fake / "projects").mkdir()
Real = cs.Scanner
claude_panel.cs.Scanner = lambda: Real(claude_dir=fake, ports_json=SP / "none.json")

STAND_IN = ('import tkinter as tk; r = tk.Tk(); r.title("Claude chat - wtdprobe - Visual Studio Code"); '
            'r.geometry("500x300+300+300"); r.mainloop()')
# VS Code (Electron) のように、タブにされたあと一度だけ自分で窓を出し直し、元の位置へ戻る窓。
# 自動取り込みの直後に、選んでいないタブの窓が見えたまま・選んだ窓が元の位置のまま、になった (2026-11-24) のを真似る
UNRULY = """
import ctypes, tkinter as tk
u = ctypes.windll.user32
r = tk.Tk(); r.title("Claude chat - wtdunruly - Visual Studio Code"); r.geometry("500x300+900+300")
st = {"orig": None, "seen": None, "done": False}
def rect(h):
    b = (ctypes.c_long * 4)(); u.GetWindowRect(h, b); return tuple(b)
def tick():
    h = int(r.wm_frame(), 16)
    if st["orig"] is None:
        st["orig"] = rect(h)
    elif not st["done"]:
        if st["seen"] is None and (not u.IsWindowVisible(h) or rect(h) != st["orig"]):
            st["seen"] = 0
        elif st["seen"] is not None:
            st["seen"] += 1
            if st["seen"] >= 5:
                st["done"] = True
                l, t, rr, b = st["orig"]
                u.SetWindowPos(h, 0, l, t, rr - l, b - t, 0x14)
                u.ShowWindow(h, 4)
    r.after(200, tick)
r.after(200, tick)
r.mainloop()
"""


def start(code, name):
    proc = subprocess.Popen([sys.executable, "-c", code])
    deadline = time.time() + 20
    while time.time() < deadline:
        time.sleep(0.3)
        for h in w.top_windows():
            if claude_panel.vscode_title_matches(w.window_title(h), name):
                return proc, h
    proc.kill()
    sys.exit(f"{name}: stand-in window did not appear")


proc, hwnd = start(STAND_IN, "wtdprobe")
proc2, hwnd2 = start(UNRULY, "wtdunruly")
folder2 = SP / "wtdunruly"
folder2.mkdir()
pids = {w.window_pid(hwnd), w.window_pid(hwnd2)}
_real_path = w.process_path
w.process_path = lambda p: r"C:\fake\Code.exe" if p in pids else _real_path(p)  # treat them as VS Code
(fake / "ide" / "1.lock").write_text(json.dumps({"pid": w.window_pid(hwnd), "workspaceFolders": [str(folder)],
                                                  "ideName": "Visual Studio Code"}), encoding="utf-8")
(fake / "ide" / "2.lock").write_text(json.dumps({"pid": w.window_pid(hwnd2), "workspaceFolders": [str(folder2)],
                                                  "ideName": "Visual Studio Code"}), encoding="utf-8")

results = []


def check(name, ok):
    results.append(ok)
    print(("PASS " if ok else "FAIL ") + name)


d = w.Dock([], {"auto_adopt": False, "restore_session": False},
           session_path=SP / "home" / "s.json", prefs_path=SP / "home" / "p.json")
d.root.geometry("1000x700+60+60")


def tab_of():
    return next((t for t in d.tabs if t.hwnd == hwnd), None)


def step0():
    check("出し直す窓も自動でタブになった", any(t.hwnd == hwnd2 for t in d.tabs))
    hidden = all(not w.user32.IsWindowVisible(t.hwnd) for t in d.tabs if t is not d.active)
    check("選んでいないタブの窓は、自分で出てきても隠れている", hidden)
    want, have = d.body_rect(), w.rect_of(d.active.hwnd)
    check(f"選んだ窓はドックの中にある ({have} / {want})", all(abs(a - b) <= 2 for a, b in zip(want, have)))
    log = (SP / "home" / "remote.log").read_text(encoding="utf-8") if (SP / "home" / "remote.log").exists() else ""
    check("出し直しを直したことが記録に残る", "隠した" in log or "置き直した" in log)
    # 返事待ちの知らせ: 選んでいないタブは橙、選んだら選択中の色 (欄が次に取り直す前に、すぐ見る)
    back = next(t for t in d.tabs if t is not d.active)
    d.set_attention({back.hwnd})
    check("知らせ中の選んでいないタブは橙", back.button.cget("bg") == w.TAB_ATTN)
    front = d.active
    d.select(back)
    check("知らせ中でも選べば選択中の色", back.button.cget("bg") == w.TAB_ACTIVE
          and front.button.cget("bg") == w.TAB_BG)
    d.set_attention(set())
    d.select(front)
    t2 = next(t for t in d.tabs if t.hwnd == hwnd2)
    d.release(t2)
    proc2.kill()
    (fake / "ide" / "2.lock").unlink()
    d.root.after(4000, step1)


def step1():
    tab = tab_of()
    check("VS Code の窓が自動でタブになった", tab is not None and tab.vscode)
    check("所有者がドック", w.user32.GetWindowLongPtrW(hwnd, w.GWLP_HWNDPARENT) == d.hwnd)
    check("ほかにタブが無いので選ばれている", d.active is tab)
    check("欄でこの窓が選択中として出る", d.claude_panel.active_folder() == str(folder))
    d.release(tab)
    d.root.after(7000, step2)


def step2():
    check("外した窓は取り込み直さない", tab_of() is None)
    d.set_auto_vscode(False)
    d.ignored.discard(hwnd)
    d.root.after(5000, step3)


def step3():
    check("自動取り込みを切ると取り込まない", tab_of() is None)
    d.set_auto_vscode(True)
    d.root.after(1000, step4)


def step4():
    check("入れ直すとすぐ取り込む", tab_of() is not None)
    d.close_tools_and_quit()  # 「ツールも閉じる」でも VS Code は閉じない


d.root.after(6000, step0)
d.root.mainloop()
time.sleep(0.5)
check("「ツールも閉じる」で閉じても VS Code の窓は残る", bool(w.user32.IsWindow(hwnd)) and proc.poll() is None)
check("元の窓に戻った (見えていて所有者なし)", bool(w.user32.IsWindowVisible(hwnd))
      and w.user32.GetWindowLongPtrW(hwnd, w.GWLP_HWNDPARENT) == 0)
proc.kill()
shutil.rmtree(SP, ignore_errors=True)
print("ALL PASS" if all(results) else "SOME FAILED")
sys.exit(0 if all(results) else 1)
