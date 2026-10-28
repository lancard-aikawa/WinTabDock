# /// script
# requires-python = ">=3.11"
# dependencies = ["pillow", "numpy", "websocket-client", "zxing-cpp"]
# ///
"""遠隔でつないでいる間に外のアプリの窓を開き、タブになってスマホの画面に出るかを見る (RepoTether が開くもの)。

    uv run experiments/external_remote_probe.py explorer.exe C:\\Windows
    uv run experiments/external_remote_probe.py wt.exe -d C:\\Windows

開いた窓は最後に閉じる (WM_CLOSE)。遠隔の画面を experiments/out/external-<exe>.png に撮る。
"""

import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import check_remote as cr  # noqa: E402  (試験用のドックと VNC の接続を借りる)

td = cr.td


def steps(dock: td.Dock, cmd: list[str]) -> None:
    c = None
    adopted = 0
    try:
        dock.call_in_tk(dock.start_remote)
        while not dock.remote_server:
            time.sleep(0.1)
        time.sleep(1.0)
        c = cr.Client(dock.prefs["remote_token"])
        c.full_frame()
        c.set_size(411, 772)
        time.sleep(1.0)
        t0 = time.monotonic()
        subprocess.Popen(cmd)
        while time.monotonic() - t0 < 15 and not dock.tabs:
            time.sleep(0.2)
        time.sleep(2.0)  # 中身が描かれるのを待つ
        if dock.tabs:
            tab = dock.tabs[0]
            adopted = tab.hwnd
            print(f"タブになった: {tab.name} {td.window_title(tab.hwnd)!r} 種類 {cr.window_class(tab.hwnd)} "
                  f"{time.monotonic() - t0:.1f} 秒 選んでいる {dock.active is tab}", flush=True)
            print("層", cr.RemoteSource_layers(dock), flush=True)
        else:
            print("タブにならなかった", flush=True)
        img = c.full_frame()
        img.save(cr.OUT / f"external-{Path(cmd[0]).stem}.png")
        time.sleep(3.0)
        c.full_frame().save(cr.OUT / f"external-{Path(cmd[0]).stem}-later.png")
        c.close()
        c = None
        time.sleep(1.5)
        print("切った後のタブ", [t.name for t in dock.tabs], flush=True)
    finally:
        if c:
            c.close()
        if adopted:
            dock.call_in_tk(lambda: dock.release(dock.tabs[0]) if dock.tabs else None)
            time.sleep(0.5)
            td.user32.PostMessageW(adopted, 0x0010, 0, 0)  # WM_CLOSE
        dock.call_in_tk(dock.stop_remote)
        dock.call_in_tk(dock.quit, wait=False)


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    cr.OUT.mkdir(exist_ok=True)
    import ctypes
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
    dock = td.Dock([], {"auto_adopt": False, "restore_session": False, "remote": {"host": "127.0.0.1"}})
    dock.root.geometry("900x600+150+120")
    threading.Thread(target=lambda: (time.sleep(1.0), steps(dock, sys.argv[1:])), daemon=True).start()
    dock.root.mainloop()


if __name__ == "__main__":
    main()
