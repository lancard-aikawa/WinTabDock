# /// script
# requires-python = ">=3.11"
# dependencies = ["pillow", "numpy", "websocket-client", "zxing-cpp"]
# ///
"""Mery を試験用のドックに取り込み、遠隔 (noVNC と同じ接続) から押して、メニューや小窓がどこに出るかを記録する。

    uv run experiments/mery_remote_probe.py "x,y[,wait]" ...   # 遠隔の画面の座標を順に押す (k:<keysym> でキー)

押すたびに experiments/out/mery-<番号>.png と、Mery のプロセスの見えている窓 (種類・位置・持ち主) を出す。
Mery はこの実験が起動したものだけを最後に止める (人の Mery には触らない。動いていたら始めない)。
"""

import os
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import check_remote as cr  # noqa: E402  (試験用のドックと VNC の接続を借りる)

td, remote = cr.td, cr.remote
MERY = Path(os.environ["APPDATA"]) / "Mery" / "Mery.exe"


def mery_pids() -> set[int]:
    out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq Mery.exe", "/FO", "CSV", "/NH"], capture_output=True,
                         text=True).stdout
    return {int(line.split(",")[1].strip('"')) for line in out.splitlines() if line.startswith('"Mery.exe"')}


def windows_of(pid: int) -> list[str]:
    rows = []
    for h in cr.all_windows():
        if td.window_pid(h) == pid and td.user32.IsWindowVisible(h):
            x, y, w, hh = td.rect_of(h)
            owner = td.user32.GetWindow(h, 4)
            rows.append(f"  {h} {cr.window_class(h)!r} {td.window_title(h)!r} ({x},{y} {w}x{hh}) owner={owner}")
    return rows


def steps(dock: td.Dock, actions: list[str]) -> None:
    proc = subprocess.Popen([str(MERY)])
    hwnd = 0
    for _ in range(100):
        time.sleep(0.1)
        found = [h for h in cr.all_windows() if td.window_pid(h) in mery_pids() and td.user32.IsWindowVisible(h)
                 and td.window_title(h).endswith(" - Mery")]
        if found:
            hwnd = found[0]
            break
    pid = td.window_pid(hwnd)
    print("Mery", hwnd, pid, cr.window_class(hwnd), flush=True)
    c = None
    try:
        dock.call_in_tk(lambda: dock.adopt("mery", hwnd))
        dock.call_in_tk(lambda: dock.select(dock.tabs[0]))
        dock.call_in_tk(dock.start_remote)
        while not dock.remote_server:
            time.sleep(0.1)
        time.sleep(1.0)
        c = cr.Client(dock.prefs["remote_token"])
        c.full_frame()
        c.set_size(411, 772)
        time.sleep(1.0)
        c.full_frame().save(cr.OUT / "mery-0.png")
        print("layers", cr.RemoteSource_layers(dock), "scale", dock.remote_server.view.scale, flush=True)
        print("\n".join(windows_of(pid)), flush=True)
        for i, a in enumerate(actions, 1):
            parts = a.split(",")
            if parts[0].startswith("k:"):
                c.key(int(parts[0][2:], 16))
                wait = float(parts[1]) if len(parts) > 1 else 0.8
            else:
                c.tap(int(parts[0]), int(parts[1]))
                wait = float(parts[2]) if len(parts) > 2 else 0.8
            time.sleep(wait)
            c.full_frame().save(cr.OUT / f"mery-{i}.png")
            print(f"[{i}] {a} menus={cr.find_menus(hwnd)} placed={getattr(dock.remote_server.view, 'menus', None) and dock.remote_server.view.menus.placed}", flush=True)
            print("\n".join(windows_of(pid)), flush=True)
    finally:
        if c:
            c.close()
        time.sleep(1.5)
        # 切った後: ドックの上へ移した小窓が元の位置に戻ったか (隠れている小窓も)
        for h in cr.all_windows():
            if td.window_pid(h) == pid and cr.window_class(h).startswith("TFind"):
                print("切った後", cr.window_class(h), td.rect_of(h), "見えている" if td.user32.IsWindowVisible(h) else "隠れている",
                      flush=True)
        proc.kill()
        subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True)
        dock.call_in_tk(dock.root.destroy)


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    if mery_pids():
        raise SystemExit("Mery が動いている (人の Mery には触らないので、閉じてから流す)")
    cr.OUT.mkdir(exist_ok=True)
    import ctypes
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
    dock = td.Dock([], {"auto_adopt": False, "restore_session": False, "remote": {"host": "127.0.0.1"}})
    dock.root.geometry("900x600+150+120")
    threading.Thread(target=lambda: (time.sleep(1.0), steps(dock, sys.argv[1:])), daemon=True).start()
    dock.root.mainloop()


if __name__ == "__main__":
    main()
