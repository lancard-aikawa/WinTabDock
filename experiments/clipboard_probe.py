# /// script
# requires-python = ">=3.11"
# dependencies = ["pillow", "numpy", "websocket-client", "zxing-cpp"]
# ///
"""試験用のドックで遠隔を 127.0.0.1 で始め、URL を experiments/out/clipboard-url.txt に書いて待つ (ブラウザで開いて試す)。

    uv run experiments/clipboard_probe.py [待つ秒数]

remote.log の場所も出す。待ち終えたら閉じる。
"""

import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import check_remote as cr  # noqa: E402  (試験用のドックを借りる)

td = cr.td


def steps(dock: td.Dock, wait: float) -> None:
    try:
        dock.call_in_tk(dock.start_remote)
        while not dock.remote_server:
            time.sleep(0.1)
        (cr.OUT / "clipboard-url.txt").write_text(dock.remote_server.url, encoding="utf-8")
        print("URL を書いた", cr.HOME / "remote.log", flush=True)
        time.sleep(wait)
    finally:
        dock.call_in_tk(dock.stop_remote)
        dock.call_in_tk(dock.quit, wait=False)


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    cr.OUT.mkdir(exist_ok=True)
    dock = td.Dock([], {"auto_adopt": False, "restore_session": False, "remote": {"host": "127.0.0.1"}})
    dock.root.geometry("900x600+150+120")
    wait = float(sys.argv[1]) if len(sys.argv) > 1 else 120
    threading.Thread(target=lambda: (time.sleep(1.0), steps(dock, wait)), daemon=True).start()
    dock.root.mainloop()


if __name__ == "__main__":
    main()
