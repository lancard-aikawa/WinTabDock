"""Claude の欄に出す URL を、Claude から足す・隠す (claude_sessions.py link) と、欄の右クリックで隠すのを確かめる。

    uv run check_claude_links.py

偽のホーム (USERPROFILE) と WINTABDOCK_HOME を一時フォルダに向けるので、人の ~/.claude と控えには触らない。
欄は隠した Tk の窓の中に作るだけで、画面には何も出さない。"""
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
SP = Path(tempfile.mkdtemp(prefix="wtd-links-"))
HOME = SP / "user"
STATE = SP / "state"
os.environ["USERPROFILE"] = str(HOME)
os.environ["WINTABDOCK_HOME"] = str(STATE)
sys.path.insert(0, str(HERE))

import claude_sessions as cs  # noqa: E402

repo = SP / "repo"
(repo / "src").mkdir(parents=True)
other = SP / "other"
other.mkdir()
(HOME / ".claude" / "ide").mkdir(parents=True)
for i, f in enumerate((repo, other)):
    (HOME / ".claude" / "ide" / f"{i}.lock").write_text(json.dumps(
        {"pid": os.getpid(), "workspaceFolders": [str(f)], "ideName": "Visual Studio Code"}), encoding="utf-8")
pdir = HOME / ".claude" / "projects" / cs.project_slug(str(repo))
pdir.mkdir(parents=True)
(pdir / "s1.jsonl").write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in [
    {"type": "ai-title", "aiTitle": "試験のセッション"},
    {"type": "assistant", "cwd": str(repo), "timestamp": "2026-11-24T00:00:00Z",
     "message": {"stop_reason": "end_turn", "content": [{"type": "text", "text":
                 "開いてください: http://localhost:5173/ と http://127.0.0.1:9999/debug"}]}},
]) + "\n", encoding="utf-8")
(HOME / ".lportman").mkdir()
(HOME / ".lportman" / "ports.json").write_text(json.dumps(
    {"registry": [{"port": 18001, "name": "開発", "project": str(repo)}]}), encoding="utf-8")

results = []


def check(name, ok, extra=""):
    results.append(bool(ok))
    print(("PASS " if ok else "FAIL ") + name + (f"  ({extra})" if extra and not ok else ""))


def cli(*args, cwd=repo):
    r = subprocess.run([sys.executable, str(HERE / "claude_sessions.py"), "link", *args], cwd=cwd,
                       capture_output=True, text=True, encoding="utf-8", env=os.environ.copy())
    return r.returncode, r.stdout + r.stderr


def scan():
    wins = cs.Scanner().scan()
    return {w.name: w for w in wins}


w = scan()["repo"]
check("はじめは会話の URL がセッションの下に出る", w.session_urls(w.sessions[0]) ==
      ["http://127.0.0.1:9999/debug", "http://localhost:5173"], w.session_urls(w.sessions[0]))

rc, out = cli("add", "https://example.workers.dev/", "--name", "本番", cwd=repo / "src")
w = scan()["repo"]
check("下のフォルダから打っても、そのフォルダを開いている窓に足す", rc == 0 and w.urls[0] == ("https://example.workers.dev", "本番"),
      f"{rc} {out} {w.urls}")
check("足したものは台帳の URL より上", [u for u, _ in w.urls] == ["https://example.workers.dev", "http://localhost:18001"],
      w.urls)
check("ほかの窓には出ない", scan()["other"].urls == [])

rc, out = cli("hide", "http://localhost:5173")
w = scan()["repo"]
check("会話から拾った URL を隠せる (末尾の / の有無は同じとみなす)", w.session_urls(w.sessions[0]) ==
      ["http://127.0.0.1:9999/debug"], w.session_urls(w.sessions[0]))

rc, out = cli("hide", "http://localhost:18001")
check("台帳の URL も隠せる", [u for u, _ in scan()["repo"].urls] == ["https://example.workers.dev"])

cli("add", "http://127.0.0.1:9999/debug", "--name", "デバッグ")
w = scan()["repo"]
check("窓の下に足した URL は、セッションの下には重ねて出さない", "http://127.0.0.1:9999/debug" not in w.session_urls(w.sessions[0]))

rc, out = cli("unhide", "http://localhost:5173")
w = scan()["repo"]
check("隠したものを戻せる", "http://localhost:5173" in w.session_urls(w.sessions[0]))

rc, out = cli("hide", "http://localhost:5173", "--all", cwd=SP)
w = scan()["repo"]
check("どの窓でも隠す (--all)", rc == 0 and "http://localhost:5173" not in w.session_urls(w.sessions[0]), out)

rc, out = cli("remove", "https://example.workers.dev")
check("足したものを外せる", "https://example.workers.dev" not in [u for u, _ in scan()["repo"].urls])

rc, out = cli("add", "not a url")
check("URL でないものは断る", rc == 2, f"{rc} {out}")

rc, out = cli("add", "http://localhost:3000", cwd=SP)
check("窓の無いフォルダでは知らせて控える", rc == 0 and "無い" in out, out)

rc, out = cli("list")
check("一覧で足した・隠したが分かる", "デバッグ" in out and "18001" in out and "どの窓でも" in out, out)

# 欄の右クリックで隠す: 画面に出さない Tk の中に欄を作る
import tkinter as tk  # noqa: E402
import claude_panel  # noqa: E402


class FakeDock:
    hwnd = 1
    active = None
    tabs = []

    def adopt_vscode_windows(self, _):
        pass

    def set_attention(self, _):
        pass

    def flash_taskbar(self):
        pass


root = tk.Tk()
root.withdraw()
panel = claude_panel.ClaudePanel(FakeDock(), root, lambda h: "")
panel.visible = True


def link_texts():
    out = []

    def walk(x):
        for c in x.winfo_children():
            if isinstance(c, tk.Button) and c.cget("text").startswith("↗"):
                out.append(c.cget("text"))
            walk(c)
    walk(panel.inner)
    return out


def wait_for(cond, secs=8):
    end = time.time() + secs
    while time.time() < end:
        root.update()
        if cond():
            return True
        time.sleep(0.05)
    return False


check("欄に足した URL が名前つきで出る", wait_for(lambda: any("デバッグ" in t for t in link_texts())), link_texts())
t0 = time.time()
panel._edit_link("hide", "http://127.0.0.1:9999/debug", str(repo))
gone = wait_for(lambda: not any("9999" in t for t in link_texts()), 2.5)
check(f"欄の右クリックで隠すと、3 秒待たずに消える ({time.time() - t0:.1f} 秒)", gone, link_texts())
panel.stop()
root.destroy()

shutil.rmtree(SP, ignore_errors=True)
print("ALL PASS" if all(results) else "SOME FAILED")
sys.exit(0 if all(results) else 1)
