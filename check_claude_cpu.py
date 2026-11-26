"""Claude の欄の CPU (セッションと、それが起動したものの合計) を確かめる。

    uv run check_claude_cpu.py

偽の ~/.claude/sessions に「この試験のプロセスが Claude のセッション」と書き、子に CPU を食うプロセスを立てて、
そのセッションに数えられるかを見る。欄は隠した Tk の窓の中に作るだけで、画面には何も出さない。"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import tkinter as tk
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import proc_cpu  # noqa: E402

SP = Path(tempfile.mkdtemp(prefix="wtd-cpu-"))
SESS = SP / "sessions"
SESS.mkdir()
results = []


def check(name, ok, extra=""):
    results.append(bool(ok))
    print(("PASS " if ok else "FAIL ") + name + (f"  ({extra})" if extra and not ok else ""))


me = os.getpid()
mine = {p[0]: p for p in proc_cpu.processes()}
check("全プロセスを読める (サービスも名前が付く)", len(mine) > 50 and any(p[2] == "System" for p in mine.values()))
check("自分のプロセスと作成時刻を読める", me in mine and mine[me][3] > 0)


def write_session(pid, sid, started):
    (SESS / f"{pid}.json").write_text(json.dumps({"pid": pid, "sessionId": sid, "procStart": str(started)}),
                                      encoding="utf-8")


write_session(me, "sess-me", mine[me][3])
# pid を使い回した別のプロセス (作成時刻が違う) は数えない
other = next(p for p in mine.values() if p[0] not in (0, me) and p[3])
write_session(other[0], "sess-reused", other[3] + 1)
(SESS / "broken.json").write_text("{", encoding="utf-8")  # 読めないものは飛ばす

# CPU を回すものは 30 秒で自分で終わる (検査が途中で落ちても残らない。下の finally でも止める)。
# 2026-11-25、確かめのスクリプトが kill の前で落ち、while 1 の 2 つが残って CPU を使い続けた
BURN = "import time\ne = time.time() + 30\nwhile time.time() < e: pass"
burn = [subprocess.Popen([sys.executable, "-c", BURN], creationflags=0x08000000) for _ in range(2)]
try:
    s = proc_cpu.CpuSampler(SESS)
    time.sleep(0.5)  # 起動の分を外す
    first = s.sample()
    check("1 回目は割合を出さない", first.interval == 0 and not first.sessions)
    check("すぐ呼び直しても測り直さない (割合がぶれる)", s.sample() is first)
    time.sleep(2.0)
    snap = s.sample()
    sc = snap.sessions.get("sess-me")
    one_core = 100 / proc_cpu.NCPU
    # PC が混んでいると、busy loop も 1 コアを取れない (全コアが埋まっていて 0.3 コアほどのことがあった)。下限は緩くする
    check("子のプロセスがセッションに数えられる (0.2〜2.6 コア分)",
          sc is not None and 0.2 * one_core <= sc.cpu <= 2.6 * one_core,
          f"{sc and round(sc.cpu, 1)} / 1 コア {one_core:.1f}")
    burners = {b.pid for b in burn}
    check("重いものの一覧に、立てた 2 つが入る", sc is not None and burners <= {p.pid for p in sc.procs[:3]})
    check("一番重いものは子 (自分の claude.exe 役ではない)", sc is not None and sc.top() and sc.top().pid in burners)
    check("重いものにコマンドラインが付く", sc is not None and any("time.time() < e" in p.cmd for p in sc.procs[:3]))
    check("pid を使い回した別のプロセスは数えない", "sess-reused" not in snap.sessions)
    check("全体の割合が出る", 0 < snap.total <= 100, snap.total)
    check("全体の上位に、このセッションが入る",
          any(n == "sess-me" and w == "session" for n, _, w in snap.top), snap.top)

    # 欄の文字 (画面には出さない)
    import claude_panel  # noqa: E402

    class FakeDock:
        active, hwnd, tabs = None, 0, []

        def set_attention(self, s): pass
        def flash_taskbar(self): pass
        def adopt_vscode_windows(self, w): pass
        def focus_vscode(self, f, n): pass

    root = tk.Tk()
    root.withdraw()
    panel = claude_panel.ClaudePanel.__new__(claude_panel.ClaudePanel)
    panel.cpu = snap
    text, tip = panel.cpu_text(sc)
    check("行の文字は「⚙ CPU n%  python.exe ×2」の形", text.startswith("⚙ CPU ") and "python.exe ×" in text, text)
    check("吹き出しにコマンドラインが出る", "e = time.time() + 30" in tip, tip)
    small = proc_cpu.SessionCpu("x", 1, cpu=claude_panel.CPU_SHOW - 0.1)
    check("CPU_SHOW 未満なら出さない", panel.cpu_text(small) == ("", ""))
    panel.cpu = proc_cpu.Snapshot()
    check("まだ測れていなければ出さない", panel.cpu_text(sc) == ("", ""))
    root.destroy()
finally:
    for b in burn:
        b.kill()
    shutil.rmtree(SP, ignore_errors=True)

print("ALL PASS" if all(results) else "SOME FAILED")
sys.exit(0 if all(results) else 1)
