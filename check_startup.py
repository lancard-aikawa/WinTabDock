# /// script
# requires-python = ">=3.11"
# dependencies = ["pillow"]
# ///
"""起動時に開くものの選び方 (前回開いていたもの / 選んだものだけ / 何も開かない) を確かめる。

    uv run check_startup.py PortWho RepoTether

どの場合も、前回のタブには 2 つ目のツール (RepoTether) だけを入れておく。選び方によって開くものが変わるかを見る。
- 選んだものだけ (1 つ目のツールを選ぶ) → 1 つ目だけが開き、前回のタブの 2 つ目は起動しない
- 何も開かない → 何も起動しない
- 前回開いていたもの → 前回のタブの 2 つ目が開く
人の設定 (settings.json / session.json) とは別の、試験用のファイルを使う。ツールは起動していない状態で流す。
"""

import json
import os
import subprocess
import sys

import tooldock as td

sys.stdout.reconfigure(encoding="utf-8")
env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
first, second = sys.argv[1], sys.argv[2]
session = td.STATE_DIR / "session-selftest.json"
prefs = td.STATE_DIR / "settings-selftest.json"
results = []

cases = [
    ("選んだものだけ", {"startup": "selected", "startup_tools": [first]}, [first]),
    ("何も開かない", {"startup": "none"}, []),
    ("前回開いていたもの", {"startup": "session"}, [second]),
]
for label, pref, expected in cases:
    td.STATE_DIR.mkdir(parents=True, exist_ok=True)
    session.write_text(json.dumps({"tabs": [second], "active": second}), encoding="utf-8")
    prefs.write_text(json.dumps(pref), encoding="utf-8")
    out = subprocess.run([sys.executable, "tooldock.py", "--selftest", "startup-check", first, second],
                         capture_output=True, text=True, encoding="utf-8", env=env)
    lines = dict(line.split(" ", 1) for line in out.stdout.splitlines() if line.startswith(("TABS ", "COUNTS ", "中止")))
    if "中止" in out.stdout or "TABS" not in lines:
        print(out.stdout.strip() or out.stderr.strip()[-800:])
        sys.exit(2)
    tabs, counts = json.loads(lines["TABS"]), json.loads(lines["COUNTS"])
    ok = tabs == expected and all((counts[n] > 0) == (n in expected) for n in (first, second))
    results.append(ok)
    print(("PASS" if ok else "FAIL") + f" {label}: 開いたタブ {tabs} / 起動数 {counts} (期待: {expected})")

session.unlink(missing_ok=True)
prefs.unlink(missing_ok=True)
print(f"\n{sum(results)}/{len(results)} PASS")
sys.exit(0 if all(results) else 1)
