"""Windows の終了・再起動のときに、取り込んだ窓を元に戻すかを確かめる。  uv run check_endsession.py [--here]

ふだんは人の画面に出ない別のデスクトップで流す (--here で人の画面)。
Tk の小さな窓を 2 つ立ててドックに取り込み、ドックに WM_QUERYENDSESSION (Windows が終わる前に送るもの) を送る。
あわせて、PC を起動し直す前の控え・別のプロセスの窓を指す控えでは、窓に触らないことを見る。"""
import ctypes
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent / "experiments"))
import hidden_desktop  # noqa: E402

if "--here" not in sys.argv and not hidden_desktop.inside():
    print(f"人の画面に出ない別のデスクトップ ({hidden_desktop.NAME}) で流す (人の画面で流すなら --here)", flush=True)
    sys.exit(hidden_desktop.rerun_inside())

SP = Path(tempfile.mkdtemp(prefix="wtd-endsession-"))
(SP / "home").mkdir()
os.environ["WINTABDOCK_HOME"] = str(SP / "home")

import wintabdock as w  # noqa: E402

results = []


def check(name, ok, extra=""):
    results.append(bool(ok))
    print(("PASS " if ok else "FAIL ") + name + (f"  ({extra})" if extra and not ok else ""), flush=True)


# 60 秒で自分で終わる窓 (検査が落ちても残らない)
PROBE = ('import tkinter as tk; r = tk.Tk(); r.title("{t}"); r.geometry("400x250+{x}+200"); '
         'r.after(60000, r.destroy); r.mainloop()')
procs = []


def start(title, x):
    p = subprocess.Popen([sys.executable, "-c", PROBE.format(t=title, x=x)])
    procs.append(p)
    for _ in range(60):
        time.sleep(0.3)
        for h in w.top_windows():
            if w.window_title(h) == title:
                return h
    sys.exit(f"{title}: window did not appear")


try:
    h1, h2 = start("wtd-end-1", 200), start("wtd-end-2", 700)
    style1 = w.user32.GetWindowLongW(h1, w.GWL_STYLE) & 0xFFFFFFFF
    d = w.Dock([], {"auto_adopt": False, "restore_session": False},
               session_path=SP / "home" / "s.json", prefs_path=SP / "home" / "p.json")
    d.root.geometry("1000x700+60+60")
    d.tools = [{"name": "一"}, {"name": "二"}]  # 前回のタブに書くのは、設定に登録したツールだけ
    answer = {}

    def adopt():
        d.adopt("一", h1)
        d.adopt("二", h2)
        d.root.after(800, before)

    def before():
        check("2 つとも取り込んだ", len(d.tabs) == 2)
        check("選んでいない方は隠れている", not w.user32.IsWindowVisible(h1))
        check("控えのファイルがある", w.STATE.exists())

        def send():
            res = ctypes.c_size_t()
            w.user32.SendMessageTimeoutW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t,
                                                     ctypes.c_uint, ctypes.c_uint, ctypes.POINTER(ctypes.c_size_t)]
            t = time.time()
            ok = w.user32.SendMessageTimeoutW(int(d.root.wm_frame(), 16), 0x0011, 0, 0, 0, 5000, ctypes.byref(res))
            answer.update(ok=ok, value=res.value, secs=time.time() - t)
        threading.Thread(target=send, daemon=True).start()
        d.root.after(1500, after)

    def after():
        check("終了してよいと答える", answer.get("ok") and answer.get("value") == 1, answer)
        check("答えるまで 1 秒かからない", answer.get("secs", 9) < 1.0, answer)
        check("タブは空になる", d.tabs == [])
        for h, n in ((h1, "一"), (h2, "二")):
            check(f"{n}: 見えている", w.user32.IsWindowVisible(h))
            check(f"{n}: 所有者なし", w.user32.GetWindowLongPtrW(h, w.GWLP_HWNDPARENT) == 0)
        check("元の枠に戻る", (w.user32.GetWindowLongW(h1, w.GWL_STYLE) & 0xFFFFFFFF) == style1)
        check("控えのファイルは消える", not w.STATE.exists())
        saved = json.loads((SP / "home" / "s.json").read_text(encoding="utf-8")) if (SP / "home" / "s.json").exists() else {}
        check("前回のタブに 2 つとも残る (次に開いたときに並べ直す)", sorted(saved.get("tabs", [])) == ["一", "二"], saved)
        check("ドックは開いたまま", bool(w.user32.IsWindow(int(d.root.wm_frame(), 16))))
        d.root.after(200, d.quit)

    d.root.after(500, adopt)
    d.root.mainloop()

    # 控えから戻す (rescue): 起動し直す前の控えと、別のプロセスの窓を指す控えは、窓に触らない
    before_style = w.user32.GetWindowLongW(h1, w.GWL_STYLE) & 0xFFFFFFFF
    bogus = {"hwnd": h1, "style": 0x80000000, "exstyle": 0x80, "rect": [0, 0, 10, 10], "owner": 0, "maximized": False}
    old = w.state_path(4000001)
    old.write_text(json.dumps([bogus | {"pid": w.window_pid(h1)}]), encoding="utf-8")
    t = w.boot_time() - 60
    os.utime(old, (t, t))
    other = w.state_path(4000002)
    other.write_text(json.dumps([bogus | {"pid": w.window_pid(h1) + 1}]), encoding="utf-8")
    n = w.rescue()
    after_style = w.user32.GetWindowLongW(h1, w.GWL_STYLE) & 0xFFFFFFFF
    check("起動し直す前の控え・別のプロセスの控えでは窓に触らない", n == 0 and after_style == before_style,
          (n, hex(before_style), hex(after_style)))
    check("使わなかった控えは消す", not old.exists() and not other.exists())
    mine = w.state_path(4000003)
    real = {"hwnd": h2, "style": w.user32.GetWindowLongW(h2, w.GWL_STYLE) & 0xFFFFFFFF,
            "exstyle": w.user32.GetWindowLongW(h2, w.GWL_EXSTYLE) & 0xFFFFFFFF, "rect": list(w.rect_of(h2)),
            "owner": 0, "maximized": False, "pid": w.window_pid(h2)}
    mine.write_text(json.dumps([real]), encoding="utf-8")
    check("いまの控えは今までどおり戻す", w.rescue() == 1)
    real.pop("pid")
    mine.write_text(json.dumps([real]), encoding="utf-8")
    check("pid の無い古い形の控えも戻す", w.rescue() == 1)
finally:
    for p in procs:
        p.kill()
    shutil.rmtree(SP, ignore_errors=True)

print("ALL PASS" if all(results) else "SOME FAILED")
sys.exit(0 if all(results) else 1)
