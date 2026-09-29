# /// script
# requires-python = ">=3.11"
# dependencies = ["pillow"]
# ///
"""README とサイト用の画面を撮る。架空のデモアプリ 3 つをドックに取り込んで撮る。

    uv run demo/shoot.py

人のツールの中身 (プロセス一覧やフォルダの場所) が写らないように、デモアプリだけを使う。
撮影用のドックは自動取り込みを切り、前回のタブも設定も一時ファイルなので、人のドックや設定には触れない。
画像は docs/images/dock.png。終わったらデモアプリも閉じる。
"""

import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import tooldock as td  # noqa: E402
from PIL import ImageGrab  # noqa: E402

td.ctypes.windll.shcore.SetProcessDpiAwareness(2)
OUT = ROOT / "docs" / "images"

COMMON = "import ctypes, tkinter as t; ctypes.windll.shcore.SetProcessDpiAwareness(2); r=t.Tk(); r.configure(bg='#f7f7f5'); "
APPS = {
    "Todo": COMMON + (
        "r.title('Todo'); f=('Yu Gothic UI', 11); "
        "t.Label(r, text='今日やること', font=('Yu Gothic UI', 14, 'bold'), bg='#f7f7f5').pack(anchor='w', padx=16, pady=(14, 6)); "
        "items=[('リリースノートを書く', 1), ('ポートの予約を確認する', 1), ('スクリーンショットを撮り直す', 0), "
        "('依存の更新を見る', 0), ('週報を出す', 0)]; "
        # 変数は r に持たせて残す (その場で作るだけだと片付けられ、チェックが外れて写った)
        "r.vs=[t.IntVar(r, v) for _, v in items]; "
        "[t.Checkbutton(r, text=s, font=f, bg='#f7f7f5', anchor='w', variable=var).pack(fill='x', padx=16, pady=2) "
        "for (s, _), var in zip(items, r.vs)]; r.mainloop()"),
    "メモ": COMMON + (
        "r.title('メモ'); x=t.Text(r, font=('Yu Gothic UI', 11), bd=0, padx=14, pady=12); x.pack(fill='both', expand=True); "
        "x.insert('1.0', '打ち合わせメモ\\n\\n- 次の版は 10 月中旬\\n- 設定画面を分ける\\n- 画面の見本は架空のデータで撮る\\n'); "
        "r.mainloop()"),
    "タイマー": COMMON + (
        "r.title('タイマー'); t.Label(r, text='25:00', font=('Segoe UI', 64, 'bold'), bg='#f7f7f5', fg='#333').pack(pady=(40, 10)); "
        "b=t.Frame(r, bg='#f7f7f5'); b.pack(); [t.Button(b, text=s, width=8).pack(side='left', padx=4) for s in ('開始', 'リセット')]; "
        "r.mainloop()"),
}


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp())
    dock = td.Dock([], {"auto_adopt": False, "restore_session": False}, session_path=tmp / "s.json",
                   prefs_path=tmp / "p.json")
    dock.root.geometry("820x520+120+120")
    procs = {name: subprocess.Popen([sys.executable, "-c", code], stdin=subprocess.DEVNULL,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) for name, code in APPS.items()}

    def adopt_all():
        for name, p in procs.items():
            hwnd = None
            for _ in range(50):
                dock.root.update()
                time.sleep(0.1)
                hwnd = td.find_window(td.process_tree(p.pid))
                if hwnd:
                    break
            dock.adopt(name, hwnd)
        dock.select(dock.tabs[0])  # Todo を表に
        dock.root.after(1500, shoot)

    def shoot():
        # 見えている枠だけ (影や後ろの窓を含めない)
        r = td.wintypes.RECT()
        ctypes = td.ctypes
        ctypes.windll.dwmapi.DwmGetWindowAttribute(td.wintypes.HWND(dock.hwnd), 9, ctypes.byref(r), ctypes.sizeof(r))
        ImageGrab.grab(bbox=(r.left, r.top, r.right, r.bottom), all_screens=True).save(OUT / "dock.png")
        print(OUT / "dock.png")
        for t in list(dock.tabs):
            dock.close_tab(t)
        dock.root.after(1500, finish)

    def finish():
        dock.quit()
        for p in procs.values():
            if p.poll() is None:
                p.kill()

    dock.root.after(500, adopt_all)
    dock.root.mainloop()


if __name__ == "__main__":
    main()
