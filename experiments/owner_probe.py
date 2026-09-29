# /// script
# requires-python = ">=3.11"
# dependencies = ["pillow"]
# ///
"""実験: ツールの窓の所有者 (owner) をドックにしたらどうなるか。

    uv run experiments/owner_probe.py <ツールの exe>

子プロセスでドック役の Tk 窓を作り、ツールの窓の所有者にする。確かめること:
  1. ドックが前面 (タイトルバーを押した直後など) でも、ツールが手前に残るか
  2. ドックの子プロセスが異常終了 (os._exit) したとき、ツールの窓とプロセスが生き残るか
最後にツールは閉じる。
"""

import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import wintabdock as td  # noqa: E402

DOCK_CHILD = r"""
import ctypes, sys, time, tkinter as tk
sys.path.insert(0, sys.argv[2])
import wintabdock as td
ctypes.windll.shcore.SetProcessDpiAwareness(2)
tool = int(sys.argv[1])
r = tk.Tk(); r.title('owner-probe dock'); r.geometry('900x600+200+150'); r.configure(bg='#0d0d1a')
r.update()
dock = td.user32.GetAncestor(r.winfo_id(), td.GA_ROOT)
GWLP_HWNDPARENT = -8
td.user32.SetWindowLongPtrW(tool, GWLP_HWNDPARENT, dock)
td.user32.SetWindowPos(tool, None, 210, 200, 880, 540, td.SWP_NOZORDER | td.SWP_NOACTIVATE)
print('DOCK', dock, flush=True)
def top_at():
    return td.user32.GetAncestor(td.user32.WindowFromPoint(td.wintypes.POINT(600, 450)), td.GA_ROOT) == tool
def step1():
    print('付け替えた直後: TOOL_ON_TOP', top_at(), flush=True)
    # 所有者の重なり順を一度動かす (所有される窓は所有者と一緒に動くはず)
    td.user32.SetWindowPos(dock, 0, 0, 0, 0, 0, td.SWP_NOMOVE | td.SWP_NOSIZE)
    r.after(400, step2)
def step2():
    print('ドックを HWND_TOP にしたあと: TOOL_ON_TOP', top_at(), flush=True)
    # 本物のマウスでドックのタイトルバーを押したまま (ドラッグ開始の状態) で見る
    td.user32.SetCursorPos(500, 165)
    td.user32.mouse_event(0x0002, 0, 0, 0, 0)
    r.after(400, step3)
def step3():
    fg = td.user32.GetForegroundWindow()
    print('タイトルバーを押している間: FG_IS_DOCK', fg == dock, 'TOOL_ON_TOP', top_at(), flush=True)
    td.user32.mouse_event(0x0004, 0, 0, 0, 0)
    r.after(400, step4)
def step4():
    print('離したあと: TOOL_ON_TOP', top_at(), flush=True)
    r.after(300, lambda: td.os._exit(3))  # 後片付けせずに落ちる
r.after(800, step1)
r.mainloop()
"""


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    td.ctypes.windll.shcore.SetProcessDpiAwareness(2)
    exe = sys.argv[1]
    proc = subprocess.Popen([exe], cwd=str(Path(exe).parent))
    tool = None
    for _ in range(100):
        time.sleep(0.2)
        tool = td.find_window(td.process_tree(proc.pid))
        if tool:
            break
    print(f"ツールの窓 hwnd={tool}")
    child = subprocess.run([sys.executable, "-c", DOCK_CHILD, str(tool), str(Path(td.__file__).parent)],
                           capture_output=True, text=True, encoding="utf-8", timeout=60,
                           env={**td.os.environ, "PYTHONIOENCODING": "utf-8"})  # 既定の cp932 だと読めない
    print(child.stdout.strip())
    if child.stderr.strip():
        print("stderr:", child.stderr.strip()[-500:])
    time.sleep(1)
    alive_window = bool(td.user32.IsWindow(tool))
    alive_proc = any(p == td.window_pid(tool) for p in td.all_pids()) if alive_window else False
    print(f"ドックの異常終了後: ツールの窓 {'残っている' if alive_window else '消えた'}"
          f" / 表示 {'見える' if td.user32.IsWindowVisible(tool) else '見えない'}"
          f" / 所有者 {td.user32.GetWindow(tool, td.GW_OWNER) or 'なし'}"
          f" / ツールのプロセス {'生きている' if proc.poll() is None else f'終了 ({proc.returncode})'}")
    if alive_window:
        td.user32.PostMessageW(tool, td.WM_CLOSE, 0, 0)
    time.sleep(1)
    if proc.poll() is None:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True)


if __name__ == "__main__":
    main()
