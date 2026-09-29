# /// script
# requires-python = ">=3.11"
# dependencies = ["pillow"]
# ///
"""ドックが異常終了したあとに --rescue で窓が戻るかを確かめる。

    uv run check_rescue.py LPortMan

1. ツールを取り込んだまま os._exit で落とす (selftest crash)
2. 取り込まれた窓が「枠なし・タスクバー無し」のまま残っていることを見る
3. --rescue で元の枠・タスクバーの設定に戻り、見えることを見る
4. テストで起動したツールを閉じる
"""

import subprocess
import sys
import time

import tooldock as td

sys.stdout.reconfigure(encoding="utf-8")
name = sys.argv[1]
out = subprocess.run([sys.executable, "tooldock.py", "--selftest", "crash", name], capture_output=True, text=True,
                     encoding="utf-8", env={**td.os.environ, "PYTHONIOENCODING": "utf-8"})
lines = dict(line.split(" ", 1) for line in out.stdout.splitlines() if line.startswith(("HWND ", "PID ")))
if "HWND" not in lines:
    print(out.stdout.strip() or out.stderr.strip())  # 試験の前の確認で止まった (中止: ...) など
    sys.exit(2)
hwnd, crashed = int(lines["HWND"]), int(lines["PID"])
state = td.state_path(crashed)
print(f"落ちた (終了コード {out.returncode})。取り込まれていた窓 hwnd={hwnd}")

ex = td.user32.GetWindowLongPtrW(hwnd, td.GWL_EXSTYLE)
style = td.user32.GetWindowLongPtrW(hwnd, td.GWL_STYLE)
print(f"落ちた直後: タイトルバー {'あり' if style & td.WS_CAPTION == td.WS_CAPTION else 'なし'} / "
      f"タスクバー {'出ない' if ex & td.WS_EX_TOOLWINDOW else '出る'} / 控え {'あり' if state.exists() else 'なし'}")

td.ctypes.windll.shcore.SetProcessDpiAwareness(2)
print(f"--rescue: {td.rescue()} 個戻した")
ex = td.user32.GetWindowLongPtrW(hwnd, td.GWL_EXSTYLE)
style = td.user32.GetWindowLongPtrW(hwnd, td.GWL_STYLE)
ok = (style & td.WS_CAPTION == td.WS_CAPTION and not ex & td.WS_EX_TOOLWINDOW
      and td.user32.IsWindowVisible(hwnd) and not state.exists())
print(("PASS" if ok else "FAIL") + " 元の枠・タスクバーに戻り、見えている / 控えも消えた")

time.sleep(1)
td.user32.PostMessageW(hwnd, td.WM_CLOSE, 0, 0)
sys.exit(0 if ok else 1)
