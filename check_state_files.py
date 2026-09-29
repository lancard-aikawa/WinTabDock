"""控えのファイルがドックごとに分かれていて、動いているドックの控えに触らないことを確かめる。

    uv run check_state_files.py

実在しない窓の番号 (hwnd) だけを書いた偽の控えを使うので、どの窓にも触れない。
- 動いているプロセスの番号の控え → rescue() で残る
- 終了したプロセスの番号の控え → rescue() で片付く
"""

import json
import os
import sys

import tooldock as td

sys.stdout.reconfigure(encoding="utf-8")
td.STATE_DIR.mkdir(parents=True, exist_ok=True)
alive = os.getppid()  # 動いているプロセス (このスクリプトを起動した親)
dead = max(td.all_pids()) + 100_000  # まず使われていない番号
fake = [{"hwnd": 0x7FFFFFF0, "style": 0, "exstyle": 0, "rect": [0, 0, 1, 1], "owner": 0}]
for pid in (alive, dead):
    td.state_path(pid).write_text(json.dumps(fake), encoding="utf-8")

td.rescue()
ok_alive = td.state_path(alive).exists()
ok_dead = not td.state_path(dead).exists()
print(("PASS" if ok_alive else "FAIL") + f" 動いているプロセス ({alive}) の控えには触らない")
print(("PASS" if ok_dead else "FAIL") + " 終了したプロセスの控えは片付ける")
td.state_path(alive).unlink(missing_ok=True)
sys.exit(0 if ok_alive and ok_dead else 1)
