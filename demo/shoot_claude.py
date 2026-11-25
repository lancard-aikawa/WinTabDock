# /// script
# requires-python = ">=3.11"
# dependencies = ["pillow"]
# ///
"""README 用に Claude の欄を撮る。架空の VS Code の窓・セッション・リポジトリ・URL だけを載せる。

    uv run demo/shoot_claude.py

人の会話の記録やリポジトリが写らないよう、ホーム (USERPROFILE)・控え (WINTABDOCK_HOME)・一時フォルダを全部
一時フォルダの中の架空のものに向けてから読み込む。git のリポジトリも一時フォルダに作る。
画像は docs/images/claude-panel.png。撮る間の数秒だけ、撮影用のドックが画面に出る。
画面は写さず、ドックと取り込んだ窓をそれぞれ PrintWindow で描かせて重ねる (人の窓が上にあっても写らない)。
CPU の行を見せるため、架空の ~/.claude/sessions に claude.exe の代わりのプロセスを書き、node を 2 つ回させる。
欄の見出しの「CPU nn%」は、撮った PC の本当の値。
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

import proc_cpu  # noqa: E402  (USERPROFILE を架空のホームに向けた後で読む。セッションの対応も架空の ~/.claude/sessions を見る)

# 「注文 API に在庫の確認を足す」(s3) が npm test を走らせている、を見せる: claude.exe の代わりのプロセスが node を 2 つ回す
BURN = shutil.which("node")
STAND_IN = ("import subprocess, sys, time; "
            f"cmd = {[BURN, '-e', 'for(;;){}'] if BURN else [sys.executable, '-c', 'while 1: pass']!r}; "
            "ps = [subprocess.Popen(cmd, creationflags=0x08000000) for _ in range(2)]; time.sleep(120)")


def window_image(hwnd, visible):
    """窓を PrintWindow で描かせた画像 (ほかの窓が上に重なっていても、その窓だけ写る)。visible: 写す範囲 (画面の座標)"""
    from PIL import Image
    c = td.ctypes
    u, g = c.windll.user32, c.windll.gdi32
    for f, res, args in ((u.GetWindowDC, c.c_void_p, [c.c_void_p]), (g.CreateCompatibleDC, c.c_void_p, [c.c_void_p]),
                         (g.CreateCompatibleBitmap, c.c_void_p, [c.c_void_p, c.c_int, c.c_int]),
                         (g.SelectObject, c.c_void_p, [c.c_void_p, c.c_void_p]),
                         (u.PrintWindow, c.c_int, [c.c_void_p, c.c_void_p, c.c_uint]),
                         (g.GetDIBits, c.c_int, [c.c_void_p, c.c_void_p, c.c_uint, c.c_uint, c.c_void_p, c.c_void_p, c.c_uint]),
                         (g.DeleteObject, c.c_int, [c.c_void_p]), (g.DeleteDC, c.c_int, [c.c_void_p]),
                         (u.ReleaseDC, c.c_int, [c.c_void_p, c.c_void_p])):
        f.restype, f.argtypes = res, args
    wr = td.wintypes.RECT()
    u.GetWindowRect(td.wintypes.HWND(hwnd), c.byref(wr))
    w, h = wr.right - wr.left, wr.bottom - wr.top
    wdc = u.GetWindowDC(hwnd)
    mdc = g.CreateCompatibleDC(wdc)
    bmp = g.CreateCompatibleBitmap(wdc, w, h)
    g.SelectObject(mdc, bmp)
    u.PrintWindow(hwnd, mdc, 2)  # PW_RENDERFULLCONTENT
    hdr = (c.c_uint32 * 10)(40, w, (-h) & 0xFFFFFFFF, 1 | (32 << 16), 0, 0, 0, 0, 0, 0)  # BITMAPINFOHEADER (上から下)
    buf = c.create_string_buffer(w * h * 4)
    g.GetDIBits(mdc, bmp, 0, h, buf, hdr, 0)
    g.DeleteObject(bmp)
    g.DeleteDC(mdc)
    u.ReleaseDC(hwnd, wdc)
    img = Image.frombuffer("RGBA", (w, h), buf.raw, "raw", "BGRA", 0, 1).convert("RGB")
    return img.crop((visible.left - wr.left, visible.top - wr.top, visible.right - wr.left, visible.bottom - wr.top))


def start_stand_in():
    p = subprocess.Popen([sys.executable, "-c", STAND_IN], creationflags=0x08000000)
    started = next(x[3] for x in proc_cpu.processes() if x[0] == p.pid)
    (HOME / ".claude" / "sessions").mkdir(exist_ok=True)
    (HOME / ".claude" / "sessions" / f"{p.pid}.json").write_text(
        json.dumps({"pid": p.pid, "sessionId": "s3", "procStart": str(started)}), encoding="utf-8")
    return p


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
    stand_in = start_stand_in()
    busy = HOME / ".claude" / "projects" / cs.project_slug(str(shop)) / "s2.jsonl"

    def keep_busy():
        # 「商品ページ…」(s2) を動作中に見せ続ける (CPU の行が出るまで待つと、書いてから 15 秒を過ぎて動作中でなくなる)
        os.utime(busy, None)
        dock.root.after(2000, keep_busy)
    keep_busy()

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
        ready = (any("未push" in str(t) for t in labels) and any("⚙ CPU" in str(t) for t in labels)
                 and len(panel.windows) == 3)
        if not ready and tries[0] < 40:
            dock.root.after(250, wait_ready)
            return
        # 動いていたセッションが返事を終えた、の知らせを出しておく (撮るときに橙の行を見せる)
        panel.attention.add((str(shop), "s1"))
        dock.root.after(800, shoot)

    def shoot():
        # 画面を写さず、ドックとメモの窓をそれぞれ描かせて重ねる。画面を写すと、人の窓が上に重なっていればそれが写る
        # (2026-11-25、エクスプローラーと人のメモアプリの中身が写った)
        r = td.wintypes.RECT()
        ctypes = td.ctypes
        ctypes.windll.dwmapi.DwmGetWindowAttribute(td.wintypes.HWND(dock.hwnd), 9, ctypes.byref(r), ctypes.sizeof(r))
        img = window_image(dock.hwnd, r)
        for t in dock.tabs:
            if ctypes.windll.user32.IsWindowVisible(t.hwnd):
                tr = td.wintypes.RECT()
                ctypes.windll.user32.GetWindowRect(t.hwnd, ctypes.byref(tr))
                img.paste(window_image(t.hwnd, tr), (tr.left - r.left, tr.top - r.top))
        out = OUT / "claude-panel.png"
        img.save(out)
        print(out)
        for t in list(dock.tabs):
            dock.close_tab(t)
        dock.root.after(1500, finish)

    def finish():
        panel.stop()
        dock.quit()
        if proc.poll() is None:
            proc.kill()
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(stand_in.pid)], capture_output=True,
                       creationflags=0x08000000)  # 代わりのプロセスと、回している node
        shutil.rmtree(TMP, ignore_errors=True)

    dock.root.after(500, adopt)
    dock.root.mainloop()


if __name__ == "__main__":
    main()
