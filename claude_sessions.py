"""Claude Code (VS Code の拡張) のセッションを、開いている VS Code の窓ごとにまとめる。

標準ライブラリだけ。Tk にも Win32 にも触らないので、別のスレッドからでも、単独でも動く。

    uv run claude_sessions.py            # 今の一覧を文字で出す
    uv run claude_sessions.py --json     # 同じものを JSON で

読むもの (どれも読むだけ):
- ~/.claude/ide/*.lock          開いている VS Code の窓と、そのフォルダ (拡張が窓ごとに 1 つ書く)
- ~/.claude/projects/<名>/*.jsonl  セッションの記録。題名・最後に動いた時刻・編集したファイル・会話に出た URL
- ~/.lportman/ports.json       台帳に予約したポート (そのリポジトリの開発サーバの URL として出す)
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

HOME = Path(os.environ.get("USERPROFILE") or Path.home())
CLAUDE_DIR = HOME / ".claude"
PORTS_JSON = HOME / ".lportman" / "ports.json"

RECENT_SECONDS = 3 * 24 * 3600  # これより前に止まったセッションは出さない
BUSY_SECONDS = 15  # 記録がこの秒数のうちに増えていれば「動作中」
MAX_SESSIONS_PER_WINDOW = 6
EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
URL_RE = re.compile(r"https?://[A-Za-z0-9\-._~:/?#\[\]@!$&'*+,;=%]+")
URL_TRAIL = ".,;:)]}>'\"`"
# 「見る URL」として残すもの: 手元の開発サーバと、自分で出す先。会話に出た記事や資料の URL は出さない
LOCAL_HOSTS = ("localhost", "127.0.0.1", "0.0.0.0", "[::1]")
DEPLOY_SUFFIXES = (".workers.dev", ".pages.dev", ".web.app", ".firebaseapp.com", ".netlify.app",
                   ".vercel.app")
# 「編集」に出さない場所 (作業用の一時フォルダ・Claude の設定やメモリ)
SKIP_EDIT_DIRS = tuple(os.path.normcase(str(p)) for p in (
    Path(os.environ.get("TEMP") or HOME / "AppData" / "Local" / "Temp"), HOME / "AppData" / "Local" / "Temp",
    CLAUDE_DIR))


def project_slug(folder: str) -> str:
    """フォルダ → ~/.claude/projects の下のフォルダ名 (英数字以外を - に)。大文字小文字は揃わないので比べるときは無視する"""
    return re.sub(r"[^A-Za-z0-9]", "-", folder)


def git_root(path: str, _cache: dict[str, str | None] = {}) -> str | None:
    """path を含む git のリポジトリの根。無ければ None"""
    p = Path(path)
    d = p.parent if p.suffix or not p.is_dir() else p
    key = os.path.normcase(str(d))
    if key in _cache:
        return _cache[key]
    found = None
    cur = d
    for _ in range(40):
        if (cur / ".git").exists():
            found = str(cur)
            break
        if cur.parent == cur:
            break
        cur = cur.parent
    _cache[key] = found
    return found


def edited_repo(path: str) -> str | None:
    """編集したファイル → それを含むリポジトリ。一時フォルダ・Claude の設定・git の外は None"""
    n = os.path.normcase(os.path.normpath(path))
    if any(n.startswith(d + os.sep) or n == d for d in SKIP_EDIT_DIRS):
        return None
    return git_root(path)


def is_view_url(url: str) -> bool:
    m = re.match(r"https?://([^/:?#]+|\[[^\]]+\])", url)
    if not m:
        return False
    host = m.group(1).lower()
    return host in LOCAL_HOSTS or host.endswith(DEPLOY_SUFFIXES)


def same_path(a: str, b: str) -> bool:
    return os.path.normcase(os.path.normpath(a)) == os.path.normcase(os.path.normpath(b))


@dataclass
class Session:
    id: str
    path: Path
    title: str = ""
    last_prompt: str = ""
    cwd: str = ""
    branch: str = ""
    updated: float = 0.0  # 記録のファイルの更新時刻
    state: str = ""  # "busy" (動作中) / "waiting" (返事待ち) / "stalled" (道具の途中で止まっている。許可待ちかも)
    edited: dict[str, float] = field(default_factory=dict)  # 編集したリポジトリ (無ければフォルダ) → 最後に編集した時刻
    urls: list[str] = field(default_factory=list)  # 新しい順
    _offset: int = 0
    _size: int = -1
    _last_kind: str = ""  # 最後の会話の行: "end" (返事を終えた) / "tool" (道具を呼んだ・結果を返した) / "user"

    def to_json(self) -> dict:
        return {"id": self.id, "title": self.title, "last_prompt": self.last_prompt, "cwd": self.cwd,
                "branch": self.branch, "updated": self.updated, "state": self.state,
                "edited": sorted(self.edited, key=lambda k: -self.edited[k]), "urls": self.urls}


@dataclass
class Window:
    folder: str  # VS Code で開いているフォルダ
    name: str  # フォルダの名前 (窓の題名の真ん中に出るもの)
    pid: int  # VS Code のプロセス (どの窓も同じ)
    sessions: list[Session] = field(default_factory=list)
    urls: list[tuple[str, str]] = field(default_factory=list)  # LPortMan の台帳: (URL, 用途)

    def to_json(self) -> dict:
        return {"folder": self.folder, "name": self.name, "pid": self.pid,
                "sessions": [s.to_json() for s in self.sessions], "urls": self.urls}


def _iso_to_epoch(ts: str) -> float:
    try:
        return time.mktime(time.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S")) - time.timezone
    except (ValueError, TypeError):
        return 0.0


class Scanner:
    """記録は大きい (数 MB) ので、ファイルごとに読んだ所を覚えて、増えた分だけ読む。"""

    def __init__(self, claude_dir: Path = CLAUDE_DIR, ports_json: Path = PORTS_JSON):
        self.claude_dir = claude_dir
        self.ports_json = ports_json
        self.sessions: dict[str, Session] = {}  # ファイルのパス → Session

    # ── 窓 ──
    def windows(self) -> list[Window]:
        out: list[Window] = []
        seen: set[str] = set()
        for lock in sorted((self.claude_dir / "ide").glob("*.lock")):
            try:
                d = json.loads(lock.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if d.get("ideName") != "Visual Studio Code" or not _pid_alive(int(d.get("pid") or 0)):
                continue
            for folder in d.get("workspaceFolders") or []:
                key = os.path.normcase(folder)
                if key in seen:
                    continue
                seen.add(key)
                out.append(Window(folder=folder, name=Path(folder).name, pid=int(d["pid"])))
        return out

    # ── 全体 ──
    def scan(self, now: float | None = None) -> list[Window]:
        now = now or time.time()
        wins = self.windows()
        dirs = {p.name.lower(): p for p in (self.claude_dir / "projects").iterdir() if p.is_dir()} \
            if (self.claude_dir / "projects").is_dir() else {}
        registry = self._registry()
        for w in wins:
            pdir = dirs.get(project_slug(w.folder).lower())
            if pdir:
                for f in pdir.glob("*.jsonl"):
                    try:
                        st = f.stat()
                    except OSError:
                        continue
                    if now - st.st_mtime > RECENT_SECONDS:
                        continue
                    s = self.sessions.get(str(f)) or Session(id=f.stem, path=f)
                    self.sessions[str(f)] = s
                    self._read(s, st.st_size, st.st_mtime, now)
                    if s.title or s.last_prompt:
                        w.sessions.append(s)
            w.sessions.sort(key=lambda s: -s.updated)
            del w.sessions[MAX_SESSIONS_PER_WINDOW:]
            roots = {w.folder} | {r for s in w.sessions for r in s.edited}
            w.urls = []
            for r in registry:
                url = f"http://localhost:{r['port']}"
                if any(same_path(r.get("project", ""), root) for root in roots) and url not in {u for u, _ in w.urls}:
                    w.urls.append((url, r.get("name", "")))
        return wins

    def _registry(self) -> list[dict]:
        try:
            return json.loads(self.ports_json.read_text(encoding="utf-8")).get("registry") or []
        except (OSError, ValueError):
            return []

    # ── 1 つの記録 ──
    def _read(self, s: Session, size: int, mtime: float, now: float) -> None:
        if size < s._size:  # 書き直された (まずない)。最初から
            s.__init__(id=s.id, path=s.path)
        if size != s._size:
            try:
                with open(s.path, "rb") as fh:
                    fh.seek(s._offset)
                    chunk = fh.read(size - s._offset)
            except OSError:
                return
            end = chunk.rfind(b"\n")
            if end >= 0:  # 書きかけの最後の行は次に回す
                for line in chunk[:end].split(b"\n"):
                    self._line(s, line)
                s._offset += end + 1
            s._size = size
        s.updated = mtime
        if now - mtime < BUSY_SECONDS:
            s.state = "busy"
        elif s._last_kind == "end":
            s.state = "waiting"
        else:
            s.state = "stalled"

    def _line(self, s: Session, raw: bytes) -> None:
        if not raw.strip():
            return
        try:
            d = json.loads(raw)
        except ValueError:
            return
        t = d.get("type")
        if t == "ai-title" and d.get("aiTitle"):
            s.title = d["aiTitle"]
        elif t == "last-prompt" and d.get("lastPrompt"):
            s.last_prompt = d["lastPrompt"]
        elif t in ("assistant", "user"):
            if d.get("isSidechain"):
                return  # サブエージェントの中の会話
            if d.get("cwd"):
                s.cwd = d["cwd"]
            if d.get("gitBranch"):
                s.branch = d["gitBranch"]
            msg = d.get("message") or {}
            content = msg.get("content")
            blocks = content if isinstance(content, list) else [{"type": "text", "text": content or ""}]
            when = _iso_to_epoch(d.get("timestamp", ""))
            for b in blocks:
                if not isinstance(b, dict):
                    continue
                bt = b.get("type")
                if t == "assistant" and bt == "text":
                    self._urls(s, b.get("text") or "")
                elif t == "assistant" and bt == "tool_use" and b.get("name") in EDIT_TOOLS:
                    fp = (b.get("input") or {}).get("file_path") or (b.get("input") or {}).get("notebook_path")
                    root = edited_repo(fp) if fp else None
                    if root:
                        s.edited[root] = when
            if t == "assistant":
                kinds = {b.get("type") for b in blocks if isinstance(b, dict)}
                if "tool_use" in kinds:
                    s._last_kind = "tool"
                elif msg.get("stop_reason") == "end_turn" or ("text" in kinds and msg.get("stop_reason") != "tool_use"):
                    s._last_kind = "end"
            else:
                s._last_kind = "tool" if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in blocks) \
                    else "user"

    @staticmethod
    def _urls(s: Session, text: str) -> None:
        for m in URL_RE.finditer(text):
            u = m.group(0).rstrip(URL_TRAIL)
            if not is_view_url(u):
                continue
            if u in s.urls:
                s.urls.remove(u)
            s.urls.insert(0, u)
        del s.urls[8:]


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform != "win32":
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    import ctypes
    h = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return False
    code = ctypes.c_ulong()
    ok = ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code))
    ctypes.windll.kernel32.CloseHandle(h)
    return bool(ok) and code.value == 259  # STILL_ACTIVE


STATE_LABEL = {"busy": "動作中", "waiting": "返事待ち", "stalled": "止まっている"}


def ago(seconds: float) -> str:
    if seconds < 60:
        return "たった今"  # 秒は出さない (欄を秒ごとに描き直すことになる)
    if seconds < 3600:
        return f"{int(seconds // 60)}分前"
    if seconds < 86400:
        return f"{int(seconds // 3600)}時間前"
    return f"{int(seconds // 86400)}日前"


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    wins = Scanner().scan()
    if "--json" in sys.argv:
        print(json.dumps([w.to_json() for w in wins], ensure_ascii=False, indent=1))
        return
    now = time.time()
    for w in wins:
        print(f"■ {w.name}  ({w.folder})")
        for url, name in w.urls:
            print(f"    URL {url}  {name}")
        for s in w.sessions:
            print(f"  ● {s.title or s.last_prompt[:30]}  [{STATE_LABEL[s.state]} {ago(now - s.updated)}]"
                  f"{'  ' + s.branch if s.branch else ''}")
            others = [r for r in s.edited if not same_path(r, w.folder)]
            for r in others:
                print(f"      編集: {r}")
            for u in s.urls[:3]:
                print(f"      URL {u}")


if __name__ == "__main__":
    main()
