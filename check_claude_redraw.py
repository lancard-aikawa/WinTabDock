"""Claude の欄が、変わった所だけ作り直すかを確かめる。  uv run check_claude_redraw.py

窓 2 つ・セッション 3 つの一覧を手で作って欄に渡し、URL・git の数・題名・状態・並びを変えるたびに、
どの部品が作り直されたか (Tk の部品の名前が変わったか) を見る。欄は隠した Tk の窓の中に作るだけで、画面には何も出さない。
一覧づくりのスレッドは空の偽の ~/.claude を読む (人の記録は読まない)。"""
import shutil
import sys
import tempfile
import time
import tkinter as tk
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import claude_panel  # noqa: E402
import claude_sessions as cs  # noqa: E402

SP = Path(tempfile.mkdtemp(prefix="wtd-redraw-"))
(SP / "claude" / "ide").mkdir(parents=True)
results = []


def check(name, ok, extra=""):
    results.append(bool(ok))
    print(("PASS " if ok else "FAIL ") + name + (f"  ({extra})" if extra and not ok else ""))


class FakeDock:
    active, hwnd, tabs = None, 0, []

    def set_attention(self, s): pass
    def flash_taskbar(self): pass
    def adopt_vscode_windows(self, w): pass
    def focus_vscode(self, f, n): pass


root = tk.Tk()
root.withdraw()
host = tk.Frame(root)
host.pack()
spacer = tk.Frame(host)
spacer.pack(side=tk.LEFT)
scanner = cs.Scanner(claude_dir=SP / "claude", ports_json=SP / "none.json", links_json=SP / "links.json")
panel = claude_panel.ClaudePanel(FakeDock(), host, lambda h: "", scanner=scanner)
panel.show(spacer)
panel.stop()  # 一覧づくりは止める (手で作った一覧を描く)

A, B = str(SP / "alpha"), str(SP / "beta")
now = time.time()


def sess(i, title, state="waiting"):
    return cs.Session(id=i, path=SP / f"{i}.jsonl", title=title, updated=now - 120, state=state, branch="main")


wa = cs.Window(folder=A, name="alpha", pid=1, sessions=[sess("a1", "題名 a1"), sess("a2", "題名 a2")])
wb = cs.Window(folder=B, name="beta", pid=1, sessions=[sess("b1", "題名 b1")])
panel.windows = [wa, wb]
panel.render()


def ids():
    """部品の名前 (作り直すと変わる)"""
    out = {}
    for f, c in panel._cards.items():
        n = Path(f).name
        out[n + ":card"] = str(c["frame"])
        out[n + ":head"] = str(c["head"])
        for sid, (row, _) in c["rows"].items():
            out[sid] = str(row)
    return out


def changed(before):
    after = ids()
    return sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))


def texts():
    out = []

    def walk(w):
        for c in w.pack_slaves():  # 画面の並び (作った順の winfo_children ではない)
            if True:
                try:
                    t = c.cget("text")
                    if t:
                        out.append(t)
                except tk.TclError:
                    pass
                walk(c)
    walk(panel.inner)
    return out


t = texts()
check("窓 2 つとセッション 3 つが出る", all(x in t for x in ("▣ alpha", "▣ beta", "題名 a1", "題名 a2", "題名 b1")), t)
order = [x for x in t if x.startswith("▣") or x.startswith("題名")]
check("並び: alpha・a1・a2・beta・b1", order == ["▣ alpha", "題名 a1", "題名 a2", "▣ beta", "題名 b1"], order)

s = ids()
panel.render()
check("何も変わらなければ作り直さない", changed(s) == [])

s = ids()
wa.sessions[0].updated, wa.sessions[0].state = now, "busy"
panel.render()
check("状態と時刻が変わっても作り直さない", changed(s) == [])
check("状態の文字は書き換わる", any(x.startswith("動作中") for x in texts()))

s = ids()
wa.sessions[1].urls = ["http://localhost:15173"]
panel.render()
check("会話に URL が出たら、そのセッションの行だけ作り直す", changed(s) == ["a2"], changed(s))
check("その URL が出る", "↗ localhost:15173" in texts())

s = ids()
wb.git = {B: cs.GitState(dirty=3)}
orig_root = cs.git_root
cs.git_root = lambda p, _c=None: B if cs.same_path(p, B) else orig_root(p)
panel.render()
check("窓の未コミットの数が変わったら、その窓の見出しだけ作り直す", changed(s) == ["beta:head"], changed(s))
check("未コミットの札が出る", "未コミット 3" in texts())

s = ids()
wa.sessions[0].title = "新しい題名"
panel.render()
check("題名が変わったら、その行だけ作り直す", changed(s) == ["a1"], changed(s))
t = texts()
order = [x for x in t if x.startswith("▣") or x.startswith("題名") or x == "新しい題名"]
check("作り直した行は元の場所に残る", order == ["▣ alpha", "新しい題名", "題名 a2", "▣ beta", "題名 b1"], order)

s = ids()
wa.sessions.insert(0, sess("a0", "題名 a0"))
panel.render()
check("新しいセッションは、その行だけ作る", changed(s) == ["a0"], changed(s))
t = texts()
order = [x for x in t if x.startswith("▣") or x.startswith("題名") or x == "新しい題名"]
check("新しいセッションは窓の先頭に出る", order[:2] == ["▣ alpha", "題名 a0"], order)

s = ids()
wb.sessions = []
panel.render()
check("消えたセッションは、その行だけ消す", changed(s) == ["b1"], changed(s))

s = ids()
wc = cs.Window(folder=str(SP / "gamma"), name="gamma", pid=1, sessions=[sess("c1", "題名 c1")])
panel.windows = [wa, wb, wc]
panel.render()
check("開いた窓は、その窓だけ作る", changed(s) == ["c1", "gamma:card", "gamma:head"], changed(s))
check("開いた窓は末尾に出る", [x for x in texts() if x.startswith("▣")][-1] == "▣ gamma")

s = ids()
panel.windows = [wa, wc]
panel.render()
check("閉じた窓は、その窓だけ消す", changed(s) == ["beta:card", "beta:head"], changed(s))

panel.attention = {(A, "a2")}
panel.render()
b = panel._title_buttons[(A, "a2")]
check("知らせ中の行は橙に塗られる (作り直した後も)", b.cget("bg") == claude_panel.ROW_ATTN, b.cget("bg"))
check("窓の行に「待ち 1」", panel._win_buttons[A].cget("text").endswith("待ち 1"))

panel.windows = []
panel.render()
check("窓が無ければ、その旨だけ出る", texts() == ["Claude の入った VS Code の窓が\n開いていません"], texts())
panel.windows = [wa]
panel.render()
check("窓が開いたら、その旨は消える", any(x.startswith("▣ alpha") for x in texts()) and not any("開いていません" in x for x in texts()), texts())

root.destroy()
shutil.rmtree(SP, ignore_errors=True)
print("ALL PASS" if all(results) else "SOME FAILED")
sys.exit(0 if all(results) else 1)
