# /// script
# requires-python = ">=3.11"
# dependencies = ["pillow"]
# ///
"""前回のタブの復元を確かめる。

    uv run check_session.py PortWho RepoTether

1. session-save: ツールを取り込み、大きさ・位置を変えてドックを閉じる (ツールは元の窓で動き続ける)
2. session-restore: 起動中のツールを取り込む (もう 1 つ起動しない)。最後にツールを閉じる
3. session-save をもう一度 → ツールを閉じる
4. session-restore: 起動していないツールを、ドックが起動して並べ直す。最後にツールを閉じる
人の前回のタブ (session.json) とは別のファイル (session-selftest.json) を使う。
"""

import os
import subprocess
import sys
import time

import wintabdock as td

sys.stdout.reconfigure(encoding="utf-8")
env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
names = sys.argv[1:]
tools, _ = td.load_config()
exes = [t["exe"] for t in tools if t["name"] in names]
if not names or len(exes) != len(names):  # 名前なしで流すと、ツール 0 個で PASS と出た
    sys.exit(f"wintabdock.toml に登録したツールの名前を渡す ({names})\n{__doc__}")


def step(mode: str, label: str) -> bool:
    out = subprocess.run([sys.executable, "wintabdock.py", "--selftest", mode, *names], capture_output=True, text=True,
                         encoding="utf-8", env=env)
    print(f"── {label} (終了コード {out.returncode})")
    print("\n".join(line for line in out.stdout.splitlines() if line.startswith(("PASS", "FAIL", "中止"))))
    ok = out.returncode == 0 and "FAIL" not in out.stdout and "中止" not in out.stdout
    if not ok and out.stderr.strip():
        print(out.stderr.strip()[-800:])
    return ok


def close_tools() -> None:
    """session-save が起動して残したツールを閉じる (この試験が起動したものだけ)。"""
    for hwnd in td.top_windows():
        if any(os.path.normcase(td.process_path(td.window_pid(hwnd))) == os.path.normcase(e) for e in exes):
            td.user32.PostMessageW(hwnd, td.WM_CLOSE, 0, 0)
    time.sleep(3)


ok = (step("session-save", "1. 保存 (ツールは動いたまま)")
      and step("session-restore", "2. 起動中のツールで開き直す")
      and step("session-save", "3. もう一度保存"))
if ok:
    close_tools()
    running = [e for e in exes if any(os.path.normcase(td.process_path(p)) == os.path.normcase(e) for p in td.all_pids())]
    print(f"── ツールを閉じた (残り: {running or 'なし'})")
    ok = not running and step("session-restore", "4. 起動していないツールを、ドックが起動して開き直す")
(td.STATE_DIR / "session-selftest.json").unlink(missing_ok=True)
sys.exit(0 if ok else 1)
