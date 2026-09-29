# /// script
# requires-python = ">=3.11"
# dependencies = ["pillow"]
# ///
"""ドックの × を押したとき、取り込んでいるツールを閉じる / 残すが設定どおりになるかを確かめる。

    uv run check_close.py PortWho RepoTether

- 一緒に閉じる → ツールが全部終わる。閉じる前の並びは前回のタブに残る
- 元の窓に戻して残す → ツールは動き続け、タスクバーに出る
- 毎回たずねる → 画面が出る。「ツールも閉じる」「残して閉じる (次からたずねない)」「キャンセル」
- × を無視するアプリ入りで「一緒に閉じる」→ 待っても閉じないものは元の窓に戻して残す (タスクバーに出る)
人の設定とは別の試験用のファイルを使う。最後に、この試験が起動したものを閉じる。
"""

import json
import os
import subprocess
import sys
import time

import tooldock as td
from selftest import count_processes, on_taskbar, taskbar_names

sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
names = sys.argv[1:]
tools = [t for t in td.load_config()[0] if t["name"] in names]
results = []


def check(ok, what):
    results.append(bool(ok))
    print(("PASS " if ok else "FAIL ") + what, flush=True)


def running():
    return {t["name"]: count_processes(t["exe"]) for t in tools}


def close_leftovers():
    for t in tools:
        for h in td.top_windows():
            if os.path.normcase(td.process_path(td.window_pid(h))) == os.path.normcase(t["exe"]):
                td.user32.PostMessageW(h, td.WM_CLOSE, 0, 0)
    time.sleep(3)


def run(case: str) -> dict:
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "TOOLDOCK_CLOSE_CASE": case}
    out = subprocess.run([sys.executable, "tooldock.py", "--selftest", "close-check", *names], capture_output=True,
                         text=True, encoding="utf-8", env=env, timeout=120)
    lines = {}
    for line in out.stdout.splitlines():
        key, _, rest = line.partition(" ")
        if key in ("TABS", "DIALOG", "AFTER_CANCEL", "STUBBORN", "中止:"):
            lines[key] = rest
    if "中止:" in lines or "TABS" not in lines:
        print(out.stdout.strip() or out.stderr.strip()[-800:])
        sys.exit(2)
    time.sleep(2)
    return lines


session = td.STATE_DIR / "session-selftest.json"
prefs = td.STATE_DIR / "settings-selftest.json"

print("── 一緒に閉じる")
r = run("close")
check(all(v == 0 for v in running().values()), f"ツールが全部終わった {running()}")
saved = json.loads(session.read_text(encoding="utf-8")) if session.exists() else {}
check(saved.get("tabs") == json.loads(r["TABS"]), f"閉じる前の並びが前回のタブに残った {saved.get('tabs')}")

print("── 元の窓に戻して残す")
run("keep")
check(all(v > 0 for v in running().values()), f"ツールは動き続けている {running()}")
for t in tools:
    check(on_taskbar(t), f"{t['name']} がタスクバーに出る")
close_leftovers()

print("── 毎回たずねる → ツールも閉じる")
r = run("ask-close")
check(r.get("DIALOG") == "True", "たずねる画面が出た")
check(all(v == 0 for v in running().values()), f"ツールが全部終わった {running()}")

print("── 毎回たずねる → 残して閉じる (次からたずねない)")
r = run("ask-keep")
check(r.get("DIALOG") == "True", "たずねる画面が出た")
check(all(v > 0 for v in running().values()), f"ツールは動き続けている {running()}")
saved_prefs = json.loads(prefs.read_text(encoding="utf-8")) if prefs.exists() else {}
check(saved_prefs.get("on_close") == "keep", f"次からたずねない → 「元の窓に戻して残す」を覚えた {saved_prefs}")
close_leftovers()

print("── 毎回たずねる → キャンセル")
r = run("ask-cancel")
tabs_before = json.loads(r["TABS"])
after = r.get("AFTER_CANCEL", "")
check(r.get("DIALOG") == "True" and after.startswith(json.dumps(tabs_before, ensure_ascii=False)) and "dock=True" in after,
      f"キャンセルするとドックもタブもそのまま ({after})")
check(all(v == 0 for v in running().values()), f"(片付けで閉じた) {running()}")

print("── × を無視するアプリ入りで、一緒に閉じる")
r = run("close-stubborn")
pid, hwnd = (int(x) for x in r["STUBBORN"].split())
check(all(v == 0 for v in running().values()), f"閉じられるツールは終わった {running()}")
style = td.user32.GetWindowLongPtrW(hwnd, td.GWL_STYLE)
check(td.user32.IsWindow(hwnd) and td.user32.IsWindowVisible(hwnd) and not td.user32.GetWindow(hwnd, td.GW_OWNER)
      and style & td.WS_CAPTION == td.WS_CAPTION, "閉じなかったアプリは元の窓に戻って見えている")
check(any("stubborn app" in b for b in taskbar_names()), "閉じなかったアプリはタスクバーに出る")
subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True)

close_leftovers()
session.unlink(missing_ok=True)
prefs.unlink(missing_ok=True)
print(f"\n{sum(results)}/{len(results)} PASS")
sys.exit(0 if all(results) else 1)
