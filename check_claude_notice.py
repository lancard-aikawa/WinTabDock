"""Claude の欄の知らせ (返事待ち・許可待ちかもに変わったら橙にする) と、git のやり残しの札を確かめる。

    uv run check_claude_notice.py

偽のホーム (USERPROFILE) と WINTABDOCK_HOME を一時フォルダに向け、git も一時フォルダのリポジトリだけを見る。
欄は隠した Tk の窓の中に作るだけで、画面には何も出さない (ドックは偽物)。"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
HERE = Path(__file__).resolve().parent
SP = Path(tempfile.mkdtemp(prefix="wtd-notice-"))
HOME = SP / "user"
os.environ["USERPROFILE"] = str(HOME)
os.environ["WINTABDOCK_HOME"] = str(SP / "state")
sys.path.insert(0, str(HERE))

import claude_sessions as cs  # noqa: E402

cs.GIT_SECONDS = 1  # 試験では待たない

results = []


def check(name, ok, extra=""):
    results.append(bool(ok))
    print(("PASS " if ok else "FAIL ") + name + (f"  ({extra})" if extra and not ok else ""))


def git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                   creationflags=0x08000000)


# ── 状態の見分け (道具を呼んだまま結果が返らない = 許可待ちかも) ──
def session_with(lines, age):
    f = SP / f"s{len(list(SP.glob('s*.jsonl')))}.jsonl"
    f.write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")
    t = time.time() - age
    os.utime(f, (t, t))
    s = cs.Session(id=f.stem, path=f)
    st = f.stat()
    cs.Scanner()._read(s, st.st_size, st.st_mtime, time.time())
    return s.state


TOOL = {"type": "assistant", "message": {"stop_reason": "tool_use", "content": [
    {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls"}}]}}
RESULT = {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]}}
END = {"type": "assistant", "message": {"stop_reason": "end_turn", "content": [{"type": "text", "text": "done"}]}}
check("道具を呼んだまま 40 秒 = 許可待ちかも", session_with([TOOL], 40) == "asking")
check("道具を呼んだまま 20 秒 = まだ知らせない (考え中か中断)", session_with([TOOL], 20) == "stalled")
check("結果を返したあと止まっている = 考え中か中断", session_with([TOOL, RESULT], 40) == "stalled")
check("返事を終えた = 返事待ち", session_with([TOOL, RESULT, END], 40) == "waiting")
check("いま書いている = 動作中", session_with([TOOL], 1) == "busy")

# ── 何を待っているか ──
check("Bash はコマンドの 1 行目", cs.describe_tool("Bash", {"command": "npm test\nsecond"})[0] == "Bash: npm test")
check("Edit はファイルの名前だけ", cs.describe_tool("Edit", {"file_path": r"C:\a\b\wintabdock.py"})[0]
      == "Edit: wintabdock.py")
check("MCP の道具は道具の名前だけ", cs.describe_tool("mcp__playwright__browser_click", {})[0] == "browser_click")
check("長いコマンドは切る", len(cs.describe_tool("Bash", {"command": "x" * 200})[0]) <= 56)


def pending_after(lines):
    f = SP / "pending.jsonl"
    f.write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")
    s = cs.Session(id="p", path=f)
    cs.Scanner()._read(s, f.stat().st_size, time.time() - 40, time.time())
    return s.waiting_on()[0]


def call(i, cmd):
    return {"type": "assistant", "message": {"stop_reason": "tool_use", "content": [
        {"type": "tool_use", "id": i, "name": "Bash", "input": {"command": cmd}}]}}


def result(i):
    return {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": i, "content": "ok"}]}}


check("結果の返っていない道具を出す", pending_after([call("a", "ls")]) == "Bash: ls")
check("結果が返ったものは出さない", pending_after([call("a", "ls"), result("a"), call("b", "pwd")]) == "Bash: pwd")
check("並べて呼んだときは最後のもの + ほかの数", pending_after([call("a", "ls"), call("b", "pwd")]) == "Bash: pwd  ほか 1")
check("並べて呼んだ片方だけ返った", pending_after([call("a", "ls"), call("b", "pwd"), result("b")]) == "Bash: ls")
check("返事を終えたら消える", pending_after([call("a", "ls"), END]) == "")
check("人が書き直したら消える (中断)", pending_after([call("a", "ls"), {"type": "user", "message": {"content": "やめて"}}])
      == "")

# ── git のやり残し ──
repo = SP / "wtdnotice"
repo.mkdir()
git("init", "-q", "-b", "main", cwd=repo)
git("config", "user.email", "t@example.invalid", cwd=repo)
git("config", "user.name", "t", cwd=repo)
(repo / "a.txt").write_text("a", encoding="utf-8")
check("リモートの無いリポジトリ: 未コミットだけ", cs.git_state(str(repo)).label() == "未コミット 1",
      cs.git_state(str(repo)))
git("add", ".", cwd=repo)
git("commit", "-q", "-m", "a", cwd=repo)
remote = SP / "remote.git"
git("init", "-q", "--bare", str(remote), cwd=SP)
git("remote", "add", "origin", str(remote), cwd=repo)
check("リモートはあるが上流なし: どのリモートにも無いコミットを数える", cs.git_state(str(repo)).label() == "未push 1",
      cs.git_state(str(repo)))
git("push", "-q", "-u", "origin", "main", cwd=repo)
check("push 済み・変更なし: 札なし", cs.git_state(str(repo)).label() == "", cs.git_state(str(repo)))
(repo / "b.txt").write_text("b", encoding="utf-8")
git("add", ".", cwd=repo)
git("commit", "-q", "-m", "b", cwd=repo)
(repo / "c.txt").write_text("c", encoding="utf-8")
check("上流あり: 未コミット 1・未push 1", cs.git_state(str(repo)).label() == "未コミット 1  未push 1",
      cs.git_state(str(repo)))
check("git でないフォルダは None", cs.git_state(str(SP)) is None)
lock_before = list((repo / ".git").glob("index.lock"))

# ── 欄: 知らせ ──
(HOME / ".claude" / "ide").mkdir(parents=True)
(HOME / ".claude" / "ide" / "1.lock").write_text(json.dumps(
    {"pid": os.getpid(), "workspaceFolders": [str(repo)], "ideName": "Visual Studio Code"}), encoding="utf-8")
pdir = HOME / ".claude" / "projects" / cs.project_slug(str(repo))
pdir.mkdir(parents=True)
old = pdir / "old.jsonl"  # 欄を開く前から返事待ちだったもの (開いたとたんに全部光らせない)
old.write_text(json.dumps({"type": "ai-title", "aiTitle": "前からの"}) + "\n" + json.dumps(END) + "\n", encoding="utf-8")
os.utime(old, (time.time() - 600, time.time() - 600))
live = pdir / "live.jsonl"
live.write_text(json.dumps({"type": "ai-title", "aiTitle": "動いている"}) + "\n" + json.dumps(TOOL) + "\n",
                encoding="utf-8")

import tkinter as tk  # noqa: E402
import claude_panel  # noqa: E402


class FakeTab:
    def __init__(self, hwnd):
        self.hwnd = hwnd


class FakeDock:
    hwnd = 1
    active = None
    tabs = [FakeTab(99)]
    marked: set = set()
    flashes = 0

    def adopt_vscode_windows(self, _):
        pass

    def set_attention(self, hwnds):
        self.marked = hwnds

    def flash_taskbar(self):
        self.flashes += 1


dock = FakeDock()
root = tk.Tk()
root.withdraw()
titles = {99: "Claude chat - wtdnotice - Visual Studio Code"}
panel = claude_panel.ClaudePanel(dock, root, lambda h: titles.get(h, ""))
panel.visible = True
looking = {"on": False}
real_looking = panel.looking_at
panel.looking_at = lambda w: looking["on"]


def wait_for(cond, secs=10):
    end = time.time() + secs
    while time.time() < end:
        root.update()
        if cond():
            return True
        time.sleep(0.05)
    return False


def win_text():
    b = panel._win_buttons.get(str(repo))
    return b.cget("text") if b else ""


def labels():
    out = []

    def walk(x):
        for c in x.winfo_children():
            if isinstance(c, tk.Label):
                out.append(c.cget("text"))
            walk(c)
    walk(panel.inner)
    return out


check("最初の一覧では、前から返事待ちのものを知らせない", wait_for(lambda: panel._win_buttons) and not panel.attention,
      panel.attention)
check("窓の下に git の札が出る", wait_for(lambda: "未コミット 1  未push 1" in labels()), labels())

# 動いていたセッションが返事を終える
live.write_text(live.read_text(encoding="utf-8") + json.dumps(END) + "\n", encoding="utf-8")
os.utime(live, (time.time() - 20, time.time() - 20))
check("動いていたものが返事待ちになったら知らせる", wait_for(lambda: (str(repo), "live") in panel.attention),
      panel.attention)
check("窓の行に「待ち 1」と出る", wait_for(lambda: "待ち 1" in win_text()), win_text())
check("窓の行が橙になる", panel._win_buttons[str(repo)].cget("bg") == claude_panel.ATTN_BG)
check("セッションの行も橙になる", panel._title_buttons[(str(repo), "live")].cget("bg") == claude_panel.ROW_ATTN)
check("ドックのその窓のタブにも知らせる", dock.marked == {99}, dock.marked)
check("タスクバーを 1 回だけ光らせる", wait_for(lambda: False, 1) or dock.flashes == 1, dock.flashes)

looking["on"] = True
check("その窓を見たら消える", wait_for(lambda: not panel.attention and dock.marked == set()), panel.attention)
check("窓の行の文字も戻る", wait_for(lambda: win_text() == "▣ wtdnotice"), win_text())
looking["on"] = False

# 見ていない間にまた動いて、許可待ちになる
live.write_text(live.read_text(encoding="utf-8") + json.dumps(TOOL) + "\n", encoding="utf-8")
wait_for(lambda: panel._prev.get((str(repo), "live")) == "busy", 6)
os.utime(live, (time.time() - 40, time.time() - 40))
check("動いたあと許可待ちかもになっても知らせる", wait_for(lambda: (str(repo), "live") in panel.attention), panel._prev)
check("知らせるたびにタスクバーを光らせる (2 回目)", dock.flashes == 2, dock.flashes)


def tool_text():
    got = panel._tool_labels.get((str(repo), "live"))
    return got[0].cget("text") if got and got[0].winfo_manager() else ""


check("許可待ちかもの行に、待っている道具が出る", wait_for(lambda: tool_text() == "▸ Bash: ls"), tool_text())
live.write_text(live.read_text(encoding="utf-8") + json.dumps(RESULT) + "\n", encoding="utf-8")
check("結果が返って動き出したら消える", wait_for(lambda: tool_text() == ""), tool_text())

# looking_at の本物: ドックの選んでいるタブが前面でなければ見ていない (前面は試験を動かしている端末)
panel.looking_at = real_looking
w = panel.windows[0]
check("前面が別の窓なら、見ていないとみなす", real_looking(w) is False)

check("git の状態を取っても index.lock を残さない", list((repo / ".git").glob("index.lock")) == lock_before)
panel.stop()
root.destroy()
shutil.rmtree(SP, ignore_errors=True)
print("ALL PASS" if all(results) else "SOME FAILED")
sys.exit(0 if all(results) else 1)
