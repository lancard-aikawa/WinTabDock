"""Claude の記録の読み方 (JSON を読む前に要らない行を外す近道) を確かめる。  uv run check_claude_parse.py

実際の記録と同じ形の行を一時フォルダに書いて読ませる。画面には何も出さない。
人の記録で元の読み方と突き合わせるときは、docs/DEVELOPMENT.md の「記録の読み方」を見る。"""
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parent))
import claude_sessions as cs  # noqa: E402

SP = Path(tempfile.mkdtemp(prefix="wtd-parse-"))
results = []


def check(name, ok, extra=""):
    results.append(bool(ok))
    print(("PASS " if ok else "FAIL ") + name + (f"  ({extra})" if extra and not ok else ""))


def j(d):
    return json.dumps(d, ensure_ascii=False, separators=(",", ":"))


def call(i, cmd, side=False):
    return j({"parentUuid": "p", "isSidechain": side, "message": {"model": "m", "id": "msg", "type": "message",
             "role": "assistant", "content": [{"type": "tool_use", "id": i, "name": "Bash", "input": {"command": cmd}}],
             "stop_reason": "tool_use"}, "type": "assistant", "cwd": "C:\\w", "gitBranch": "main",
             "timestamp": "2026-11-25T00:00:00.000Z"})


def result(i, side=False, big=0):
    # Claude Code が書く形: 先頭に parentUuid・isSidechain・promptId、content は結果のブロックで始まる
    return j({"parentUuid": "p", "isSidechain": side, "promptId": "q", "type": "user",
              "message": {"role": "user", "content": [{"tool_use_id": i, "type": "tool_result",
                                                       "content": "x" * big + ' "type":"user" '}]},
              "uuid": "u", "toolUseResult": {"stdout": "ok", "stderr": ""}})


def said(text):
    return j({"parentUuid": "p", "isSidechain": False, "promptId": "q", "type": "user",
              "message": {"role": "user", "content": text}, "uuid": "u"})


def read(lines, partial=b""):
    f = SP / f"{time.time_ns()}.jsonl"
    f.write_bytes(("\n".join(lines) + "\n").encode("utf-8") + partial)
    s = cs.Session(id=f.stem, path=f)
    cs.Scanner(claude_dir=SP)._read(s, f.stat().st_size, time.time() - 100, time.time())
    return s


s = read([j({"type": "ai-title", "aiTitle": "題名", "sessionId": "x"}),
          j({"type": "last-prompt", "lastPrompt": "最後の依頼", "sessionId": "x"}), call("toolu_A", "npm test")])
check("題名と最後の依頼を読む", (s.title, s.last_prompt) == ("題名", "最後の依頼"), (s.title, s.last_prompt))
check("呼んだ道具を待っている", list(s.pending) == ["toolu_A"] and s._last_kind == "tool_use")

check("結果の行は近道 (JSON を読まない) に乗る", cs.TOOL_RESULT_HEAD.search(result("toolu_A").encode(), 0, 1024))
check("人が書いた行は近道に乗らない", not cs.TOOL_RESULT_HEAD.search(said("toolu_ を見て").encode(), 0, 1024))
s = read([call("toolu_A", "npm test"), result("toolu_A", big=200000)])
check("結果が返れば待たない (大きな結果でも)", s.pending == {} and s._last_kind == "tool_result", (s.pending, s._last_kind))

s = read([call("toolu_A", "npm test"), call("toolu_B", "ls"), result("toolu_A")])
check("並べて呼んだうち、返った分だけ消す", list(s.pending) == ["toolu_B"], list(s.pending))

s = read([call("toolu_A", "npm test"), result("toolu_A", side=True)])
check("サブエージェントの中の結果は数えない", list(s.pending) == ["toolu_A"] and s._last_kind == "tool_use")

blob = j({"type": "api-request-blob", "sessionId": "x", "message": {"role": "user", "content": [
    {"type": "tool_result", "tool_use_id": "toolu_A", "content": "y"}]}, "aiTitle": "違う題名"})
s = read([j({"type": "ai-title", "aiTitle": "題名", "sessionId": "x"}), call("toolu_A", "npm test"), blob,
          j({"type": "file-history-snapshot", "messageId": "m", "snapshot": {"type": "user"}})])
check("api-request などの行は読まない (中に結果や題名があっても)",
      list(s.pending) == ["toolu_A"] and s.title == "題名", (list(s.pending), s.title))

s = read([call("toolu_A", "npm test"), said("やっぱりやめて")])
check("人が書いたら待たない (近道に乗らず、元どおり読む)", s.pending == {} and s._last_kind == "user", s._last_kind)

s = read([call("toolu_A", "npm test")], partial=result("toolu_A").encode("utf-8"))
check("書きかけの最後の行は読まない", list(s.pending) == ["toolu_A"], list(s.pending))
size = s.path.stat().st_size
with open(s.path, "ab") as fh:
    fh.write(b"\n")
cs.Scanner(claude_dir=SP)._read(s, s.path.stat().st_size, time.time(), time.time())
check("書き終わったら、続きから読む", s.pending == {} and s._offset == size + 1, (s.pending, s._offset, size))

s = read([j({"parentUuid": "p", "isSidechain": False, "message": {"role": "assistant", "content": [
    {"type": "tool_use", "id": "w1", "name": "Edit", "input": {"file_path": str(Path(__file__).resolve())}}],
    "stop_reason": "tool_use"}, "type": "assistant", "timestamp": "2026-11-25T01:02:03.000Z"})])
check("編集したリポジトリと時刻を読む", list(s.edited.values()) == [cs._iso_to_epoch("2026-11-25T01:02:03")], s.edited)

shutil.rmtree(SP, ignore_errors=True)
print("ALL PASS" if all(results) else "SOME FAILED")
sys.exit(0 if all(results) else 1)
