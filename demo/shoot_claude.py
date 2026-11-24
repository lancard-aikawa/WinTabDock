# /// script
# requires-python = ">=3.11"
# dependencies = ["pillow"]
# ///
"""README 用に Claude の欄を撮る。架空の VS Code の窓・セッション・リポジトリ・URL だけを載せる。

    uv run demo/shoot_claude.py

人の会話の記録やリポジトリが写らないよう、ホーム (USERPROFILE)・控え (WINTABDOCK_HOME)・一時フォルダを全部
一時フォルダの中の架空のものに向けてから読み込む。git のリポジトリも一時フォルダに作る。
画像は docs/images/claude-panel.png。撮る間の数秒だけ、撮影用のドックが画面に出る。
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "images"
TMP = Path(tempfile.mkdtemp(prefix="wtd-shoot-"))
HOME = TMP / "home"
WORK = TMP / "work"  # 架空のリポジトリを置く所 (一時フォルダの外に見せる。一時フォルダは「編集」に出さないため)
(HOME / ".claude" / "ide").mkdir(parents=True)
(HOME / ".claude" / "projects").mkdir()
WORK.mkdir()
os.environ["USERPROFILE"] = str(HOME)
os.environ["WINTABDOCK_HOME"] = str(TMP / "state")
os.environ["TEMP"] = os.environ["TMP"] = str(TMP / "tmp")  # 「編集」に出さない一時フォルダを、架空のものにする
(TMP / "tmp").mkdir()

sys.path.insert(0, str(ROOT))
import claude_sessions as cs  # noqa: E402
import wintabdock as td  # noqa: E402
from PIL import ImageGrab  # noqa: E402

td.ctypes.windll.shcore.SetProcessDpiAwareness(2)
NOW = time.time()


def git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, creationflags=0x08000000)


def repo(name, dirty=0, unpushed=0):
    d = WORK / name
    d.mkdir()
    git("init", "-q", "-b", "main", cwd=d)
    git("config", "user.email", "demo@example.invalid", cwd=d)
    git("config", "user.name", "demo", cwd=d)
    (d / "README.md").write_text(f"# {name}\n", encoding="utf-8")
    git("add", ".", cwd=d)
    git("commit", "-q", "-m", "init", cwd=d)
    if unpushed:
        remote = TMP / f"{name}.git"
        git("init", "-q", "--bare", str(remote), cwd=TMP)
        git("remote", "add", "origin", str(remote), cwd=d)
        git("push", "-q", "-u", "origin", "main", cwd=d)
        for i in range(unpushed):
            (d / f"c{i}.txt").write_text(str(i), encoding="utf-8")
            git("add", ".", cwd=d)
            git("commit", "-q", "-m", f"change {i}", cwd=d)
    for i in range(dirty):
        (d / f"wip{i}.txt").write_text("wip", encoding="utf-8")
    return d


shop = repo("web-shop", dirty=3)
api = repo("api-server", unpushed=2, dirty=1)
notes = WORK / "notes"  # 資料のフォルダ (git ではない)。ここで始めた会話が別のリポジトリを触る
notes.mkdir()
tools = repo("image-tools", dirty=2)

for i, folder in enumerate((shop, api, notes)):
    (HOME / ".claude" / "ide" / f"{i}.lock").write_text(json.dumps(
        {"pid": os.getpid(), "workspaceFolders": [str(folder)], "ideName": "Visual Studio Code"}), encoding="utf-8")
(HOME / ".lportman").mkdir()
(HOME / ".lportman" / "ports.json").write_text(json.dumps(
    {"registry": [{"port": 15173, "name": "開発サーバ", "project": str(shop)}]}), encoding="utf-8")
cs.edit_links("add", "https://demo-shop.example.workers.dev", str(shop), "プレビュー",
              path=Path(os.environ["WINTABDOCK_HOME"]) / "claude_links.json")


def session(folder, sid, title, branch, lines, age):
    d = HOME / ".claude" / "projects" / cs.project_slug(str(folder))
    d.mkdir(exist_ok=True)
    f = d / f"{sid}.jsonl"
    rows = [{"type": "ai-title", "aiTitle": title}, {"type": "last-prompt", "lastPrompt": title}]
    for x in lines:
        x = dict(x)
        x.setdefault("cwd", str(folder))
        x["gitBranch"] = branch
        rows.append(x)
    f.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8")
    os.utime(f, (NOW - age, NOW - age))


def say(s):
    return {"type": "assistant", "timestamp": "2026-11-24T00:00:00Z",
            "message": {"stop_reason": "end_turn", "content": [{"type": "text", "text": s}]}}


def call(name, args, i="t1"):
    return {"type": "assistant", "timestamp": "2026-11-24T00:00:00Z",
            "message": {"stop_reason": "tool_use", "content": [{"type": "tool_use", "id": i, "name": name, "input": args}]}}


session(shop, "s1", "カートの合計が税込みでずれる", "fix/cart-total",
        [say("直しました。http://localhost:15173/cart で確かめてください")], 12 * 60)
session(shop, "s2", "商品ページの画像を遅延読み込みにする", "perf/images",
        [call("Edit", {"file_path": str(shop / "src" / "ProductPage.tsx")})], 0)
session(api, "s3", "注文 API に在庫の確認を足す", "feature/stock",
        [call("Bash", {"command": "npm test -- orders"})], 90)
session(notes, "s4", "画像の圧縮ツールを比べて試作する", "",
        [call("Write", {"file_path": str(tools / "compare.py")}, "w1"),
         {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "w1", "content": "ok"}]}},
         say("試作を image-tools に置きました。http://127.0.0.1:18080 で比べられます")], 3 * 3600)

COMMON = "import ctypes, tkinter as t; ctypes.windll.shcore.SetProcessDpiAwareness(2); r=t.Tk(); r.configure(bg='#f7f7f5'); "
APP = COMMON + (
    "r.title('メモ'); x=t.Text(r, font=('Yu Gothic UI', 11), bd=0, padx=14, pady=12); x.pack(fill='both', expand=True); "
    "x.insert('1.0', '打ち合わせメモ\\n\\n- 次の版は 12 月\\n- カートの不具合を先に\\n- 画面の見本は架空のデータで撮る\\n'); "
    "r.mainloop()")


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    (TMP / "state").mkdir(exist_ok=True)
    (TMP / "state" / "p.json").write_text(json.dumps({"claude_panel": True, "auto_vscode": False}), encoding="utf-8")
    dock = td.Dock([], {"auto_adopt": False, "restore_session": False}, session_path=TMP / "state" / "s.json",
                   prefs_path=TMP / "state" / "p.json")
    dock.root.geometry("1040x640+120+80")
    proc = subprocess.Popen([sys.executable, "-c", APP], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
    panel = dock.claude_panel

    def adopt():
        hwnd = None
        for _ in range(50):
            dock.root.update()
            time.sleep(0.1)
            hwnd = td.find_window(td.process_tree(proc.pid))
            if hwnd:
                break
        dock.adopt("メモ", hwnd)
        dock.root.after(500, wait_ready)

    def wait_ready(tries=[0]):
        tries[0] += 1
        labels = []

        def walk(x):
            for c in x.winfo_children():
                try:
                    labels.append(c.cget("text"))
                except Exception:
                    pass
                walk(c)
        walk(panel.inner)
        ready = any("未push" in str(t) for t in labels) and len(panel.windows) == 3
        if not ready and tries[0] < 40:
            dock.root.after(250, wait_ready)
            return
        # 動いていたセッションが返事を終えた、の知らせを出しておく (撮るときに橙の行を見せる)
        panel.attention.add((str(shop), "s1"))
        dock.root.after(800, shoot)

    def shoot():
        r = td.wintypes.RECT()
        ctypes = td.ctypes
        ctypes.windll.dwmapi.DwmGetWindowAttribute(td.wintypes.HWND(dock.hwnd), 9, ctypes.byref(r), ctypes.sizeof(r))
        out = OUT / "claude-panel.png"
        ImageGrab.grab(bbox=(r.left, r.top, r.right, r.bottom), all_screens=True).save(out)
        print(out)
        for t in list(dock.tabs):
            dock.close_tab(t)
        dock.root.after(1500, finish)

    def finish():
        panel.stop()
        dock.quit()
        if proc.poll() is None:
            proc.kill()
        shutil.rmtree(TMP, ignore_errors=True)

    dock.root.after(500, adopt)
    dock.root.mainloop()


if __name__ == "__main__":
    main()
