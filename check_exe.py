# /// script
# requires-python = ">=3.11"
# ///
"""ビルドした exe (dist/ToolDock/ToolDock.exe) を動かして確かめる。

    uv run check_exe.py

人の環境に触れないよう、exe を一時フォルダにコピーし、PortWho と RepoTether だけを登録した設定で動かす。
控えと前回のタブは TOOLDOCK_HOME で一時フォルダへ向ける。確かめること:
  1. exe が起動し、窓とタスクバーにアイコンが付いている
  2. ドックの外で起動した PortWho を自動で取り込む (所有者がドックになる)
  3. ドックを閉じると PortWho が元の窓に戻り、前回のタブが一時フォルダに保存される
  4. exe をもう一度起動すると、起動中の PortWho を前回のタブとして取り込む
  5. ログイン時に起動する (HKCU Run) の登録と解除 (登録していなかったなら、最後に元どおり無い状態に戻す)
"""

import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import tooldock as td

sys.stdout.reconfigure(encoding="utf-8")
td.ctypes.windll.shcore.SetProcessDpiAwareness(2)
ROOT = Path(__file__).resolve().parent
tools, _ = td.load_config()
portwho = next(t for t in tools if t["name"] == "PortWho")
repotether = next(t for t in tools if t["name"] == "RepoTether")
results = []


def check(ok, what):
    results.append(bool(ok))
    print(("PASS " if ok else "FAIL ") + what, flush=True)


def running(exe):
    return [p for p in td.all_pids() if os.path.normcase(td.process_path(p)) == os.path.normcase(exe)]


def wait(cond, timeout=15):
    end = time.time() + timeout
    while time.time() < end:
        v = cond()
        if v:
            return v
        time.sleep(0.3)
    return None


# 人が使っているものに触れない
busy = [t["name"] for t in (portwho, repotether) if running(t["exe"])]
if busy or td.other_docks():
    print(f"中止: {', '.join(busy) or 'ほかの ToolDock'} が動いています。閉じてから流してください")
    sys.exit(2)

work = Path(tempfile.mkdtemp(prefix="tooldock-exe-"))
app = work / "ToolDock"
shutil.copytree(ROOT / "dist" / "ToolDock", app)
(app / "tooldock.toml").write_text(
    "auto_adopt = true\nrestore_session = true\n\n"
    f'[[tool]]\nname = "PortWho"\nexe = "{portwho["exe"]}"\n\n'
    f'[[tool]]\nname = "RepoTether"\nexe = "{repotether["exe"]}"\n', encoding="utf-8")
env = {**os.environ, "TOOLDOCK_HOME": str(work / "home")}
exe = str(app / "ToolDock.exe")


def dock_window():
    return next((h for h in td.other_docks() if os.path.normcase(td.process_path(td.window_pid(h))) == os.path.normcase(exe)), None)


def stop_here(proc):
    """ドックの窓が見つからないまま先へ進むと、「所有者 == ドック (なし)」が所有者のない窓と一致して
    見かけだけ通ってしまった。見つからなければ、起動した exe を止めて終わる。"""
    subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True)
    shutil.rmtree(work, ignore_errors=True)
    print(f"\n{sum(results)}/{len(results)} PASS (途中で中止)")
    sys.exit(1)


dock_proc = subprocess.Popen([exe], env=env)
dock = wait(dock_window)
check(dock, "exe が起動してドックの窓が出た")
if not dock:
    stop_here(dock_proc)
# tkinter の iconbitmap はウィンドウクラスにアイコンを付けるので、WM_GETICON は 0 を返す (それで FAIL と誤判定した)。
# クラスのアイコンが Tk の既定 (羽) ではなく自前のものかは、タイトルバーを撮って目で確かめた (2026-09-29)
get_class = td.user32.GetClassLongPtrW
get_class.restype, get_class.argtypes = td.LONG_PTR, [td.wintypes.HWND, td.ctypes.c_int]
icon = td.user32.SendMessageW(dock, 0x007F, 1, 0) or get_class(dock, -14)  # WM_GETICON / GCLP_HICON
check(icon, "ドックの窓にアイコンが付いている")


def windows_of(pid_root):
    tree = td.process_tree(pid_root)
    found = []

    def cb(h, _):
        if td.window_pid(h) in tree and td.window_title(h):
            found.append(h)
        return True

    td.user32.EnumWindows(td.WNDENUMPROC(cb), 0)
    return found


pw = subprocess.Popen([portwho["exe"]], cwd=str(Path(portwho["exe"]).parent))  # ドックを通さずに起動
adopted = wait(lambda: next((h for h in windows_of(pw.pid) if td.user32.GetWindow(h, td.GW_OWNER) == dock), None), 20)
check(adopted, "ドックの外で起動した PortWho を exe のドックが自動で取り込んだ")

td.user32.PostMessageW(dock, td.WM_CLOSE, 0, 0)
wait(lambda: dock_proc.poll() is not None, 15)
style = td.user32.GetWindowLongPtrW(adopted, td.GWL_STYLE) if adopted else 0
check(adopted and not td.user32.GetWindow(adopted, td.GW_OWNER) and style & td.WS_CAPTION == td.WS_CAPTION,
      "ドックを閉じると PortWho は元の窓に戻った")
session = work / "home" / "session.json"
saved = td.json.loads(session.read_text(encoding="utf-8")) if session.exists() else {}
check(saved.get("tabs") == ["PortWho"], f"前回のタブが TOOLDOCK_HOME に保存された ({saved})")

dock_proc = subprocess.Popen([exe], env=env)
dock = wait(dock_window)
if not dock:
    check(False, "exe をもう一度起動してドックの窓が出た")
    stop_here(dock_proc)
again =wait(lambda: td.user32.GetWindow(adopted, td.GW_OWNER) == dock, 15) if adopted and dock else None
check(again, "exe をもう一度起動すると、起動中の PortWho を前回のタブとして取り込んだ")

# ログイン時に起動する: exe の場所で登録・解除できるか (関数を exe の場所で呼ぶ)
had = None
import winreg  # noqa: E402

try:
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, td.RUN_KEY) as k:
        had = winreg.QueryValueEx(k, "ToolDock")[0]
except OSError:
    pass
if had is None:
    real = sys.executable
    sys.executable = exe
    td.set_autostart(True)
    on = td.autostart_enabled()
    td.set_autostart(False)
    off = not td.autostart_enabled()
    sys.executable = real
    check(on and off, "ログイン時に起動する: exe の場所で登録でき、解除すると消える (元どおり無い状態)")
else:
    print(f"(ログイン時の起動はもう登録されているので試さない: {had})")

# 片付け: ドックを閉じて、試験で起動した PortWho を閉じる
if dock:
    td.user32.PostMessageW(dock, td.WM_CLOSE, 0, 0)
wait(lambda: dock_proc.poll() is not None, 15)
for h in windows_of(pw.pid):
    td.user32.PostMessageW(h, td.WM_CLOSE, 0, 0)
wait(lambda: pw.poll() is not None, 10)
if pw.poll() is None:
    subprocess.run(["taskkill", "/F", "/T", "/PID", str(pw.pid)], capture_output=True)
shutil.rmtree(work, ignore_errors=True)
print(f"\n{sum(results)}/{len(results)} PASS")
sys.exit(0 if all(results) else 1)
