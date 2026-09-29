# /// script
# requires-python = ">=3.11"
# dependencies = ["pillow"]
# ///
"""ドックを閉じたあと、元に戻したツールがタスクバーに出るかを確かめる。

    uv run check_taskbar.py PortWho RepoTether

1. 比べるため: ドックを通さずに起動したツールがタスクバーに出る
2. ドックに取り込んで (タスクバーから消える)、ドックを閉じる → 元の窓に戻ったツールがタスクバーに出る
タスクバーのボタンは UI Automation で名前を読む。窓の属性 (所有者・拡張スタイル) だけ見ると、
戻っていないのに通ってしまう (以前の試験がそうだった)。最後に、この試験が起動したツールを閉じる。
"""

import os
import subprocess
import sys
import time
from pathlib import Path

import tooldock as td
from selftest import count_processes, on_taskbar, windows_of

sys.stdout.reconfigure(encoding="utf-8")
names = sys.argv[1:]
tools = [t for t in td.load_config()[0] if t["name"] in names]
env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
results = []


def check(ok, what):
    results.append(bool(ok))
    print(("PASS " if ok else "FAIL ") + what, flush=True)


def close_all():
    for t in tools:
        for h in windows_of(t["exe"]):
            td.user32.PostMessageW(h, td.WM_CLOSE, 0, 0)
    time.sleep(3)


busy = [t["name"] for t in tools if count_processes(t["exe"])]
if busy or td.other_docks():
    print(f"中止: {', '.join(busy) or 'ほかの ToolDock'} が動いています。閉じてから流してください")
    sys.exit(2)

# 1. 比べるため: ドックを通さずに起動
procs = [subprocess.Popen([t["exe"]], cwd=str(Path(t["exe"]).parent)) for t in tools]
time.sleep(6)
for t in tools:
    check(on_taskbar(t), f"ドックを通さずに起動した {t['name']} はタスクバーに出る")
close_all()

# 2. ドックに取り込んで閉じる (session-save: 取り込み → 閉じる。ツールは元の窓に戻って動き続ける)
out = subprocess.run([sys.executable, "tooldock.py", "--selftest", "session-save", *names], capture_output=True,
                     text=True, encoding="utf-8", env=env)
time.sleep(2)
for t in tools:
    check(windows_of(t["exe"]), f"ドックを閉じたあと {t['name']} の窓は見えている")
    check(on_taskbar(t), f"ドックを閉じたあと {t['name']} がタスクバーに出る")
close_all()


def dock_then_check(label: str, launch_before: bool) -> None:
    """自動取り込みで取り込ませてからドックを閉じ、タスクバーを見る。"""
    if launch_before:
        for t in tools:
            subprocess.Popen([t["exe"]], cwd=str(Path(t["exe"]).parent))
        time.sleep(6)
    dock = subprocess.Popen([sys.executable, "tooldock.py", "--selftest", "adopt-quit", *names], stdout=subprocess.PIPE,
                            text=True, encoding="utf-8", env=env)
    if not launch_before:
        time.sleep(3)  # ドックが開いてから、ドックの外で起動する
        for t in tools:
            subprocess.Popen([t["exe"]], cwd=str(Path(t["exe"]).parent))
    out, _ = dock.communicate(timeout=90)
    tabs = next((line[5:] for line in out.splitlines() if line.startswith("TABS ")), "?")
    time.sleep(2)
    print(f"── {label}: 取り込んだタブ {tabs}")
    for t in tools:
        check(on_taskbar(t), f"{label} → ドックを閉じたあと {t['name']} がタスクバーに出る")
    close_all()


dock_then_check("3. 起動中のツールをドックが開いたときに取り込んだ", launch_before=True)
dock_then_check("4. ドックが開いたあとに外で起動したツールを取り込んだ", launch_before=False)
(td.STATE_DIR / "session-selftest.json").unlink(missing_ok=True)
left = [t["name"] for t in tools if count_processes(t["exe"])]
if left:
    print(f"(閉じきれなかった: {left})")
print(f"\n{sum(results)}/{len(results)} PASS")
sys.exit(0 if all(results) else 1)
