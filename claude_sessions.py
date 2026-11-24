"""Claude Code (VS Code の拡張) のセッションを、開いている VS Code の窓ごとにまとめる。

標準ライブラリだけ。Tk にも Win32 にも触らないので、別のスレッドからでも、単独でも動く。

    uv run claude_sessions.py            # 今の一覧を文字で出す
    uv run claude_sessions.py --json     # 同じものを JSON で

欄に出す URL を足す・隠す (Claude のセッションから打ってもらうためのもの。窓は今のフォルダから決める):
    uv run claude_sessions.py link add <URL> [--name 名前] [--folder 窓のフォルダ]
    uv run claude_sessions.py link remove <URL> [--folder ...]     # 足したものを外す
    uv run claude_sessions.py link hide <URL> [--folder ... | --all]  # 会話・台帳から出たものも隠す
    uv run claude_sessions.py link unhide <URL> [--folder ... | --all]
    uv run claude_sessions.py link list [--folder ...]

読むもの (どれも読むだけ):
- ~/.claude/ide/*.lock          開いている VS Code の窓と、そのフォルダ (拡張が窓ごとに 1 つ書く)
- ~/.claude/projects/<名>/*.jsonl  セッションの記録。題名・最後に動いた時刻・編集したファイル・会話に出た URL
- ~/.lportman/ports.json       台帳に予約したポート (そのリポジトリの開発サーバの URL として出す)
書くもの:
- %LOCALAPPDATA%/WinTabDock/claude_links.json  足した URL・隠した URL (link のときだけ書く)
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
# wintabdock.STATE_DIR と同じ場所 (WINTABDOCK_HOME で変えられる)
LINKS_JSON = Path(os.environ.get("WINTABDOCK_HOME") or Path(os.environ.get("LOCALAPPDATA", ".")) / "WinTabDock") \
    / "claude_links.json"
ALL = "*"  # 隠すとき、どの窓でも隠す

RECENT_SECONDS = 3 * 24 * 3600  # これより前に止まったセッションは出さない
BUSY_SECONDS = 15  # 記録がこの秒数のうちに増えていれば「動作中」
# 道具を呼んだまま結果がこの秒数返らなければ「許可待ちかも」(長いコマンドの実行中も入る。外れることがある)
ASK_SECONDS = 30
GIT_SECONDS = 30  # git の状態を取り直す間 (3 秒ごとに git を呼ばない)
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


def describe_tool(name: str, args: dict) -> tuple[str, str]:
    """道具の呼び出し → (欄に出す短い説明, 吹き出しに出す長い説明)。例: ("Bash: npm test", "Bash\nnpm test --watch=false")"""
    args = args if isinstance(args, dict) else {}
    short_name = name.split("__")[-1] if name.startswith("mcp__") else name  # mcp__サーバ__道具 は道具の名前だけ
    detail = ""
    for key in ("command", "file_path", "notebook_path", "url", "query", "pattern", "description", "prompt", "skill"):
        v = args.get(key)
        if isinstance(v, str) and v.strip():
            detail = v.strip()
            break
    if not detail:
        return short_name, name
    if name in ("Edit", "Write", "Read", "MultiEdit", "NotebookEdit"):
        brief = Path(detail).name  # パスは名前だけ (欄が狭い)
    else:
        brief = detail.splitlines()[0]
        if len(brief) > 48:
            brief = brief[:47] + "…"
    return f"{short_name}: {brief}", f"{name}\n{detail[:400]}"


def norm_url(url: str) -> str:
    """http://localhost:5173/ と http://localhost:5173 を同じものにする (末尾の / だけのパスを落とす)"""
    return re.sub(r"^(https?://[^/?#]+)/$", r"\1", url.strip())


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
    # "busy" (動作中) / "waiting" (返事待ち) / "asking" (道具の結果が返らない。許可待ちかも) / "stalled" (考え中か中断)
    state: str = ""
    edited: dict[str, float] = field(default_factory=dict)  # 編集したリポジトリ (無ければフォルダ) → 最後に編集した時刻
    urls: list[str] = field(default_factory=list)  # 新しい順
    _offset: int = 0
    _size: int = -1
    # 結果がまだ返っていない道具の呼び出し: id → (短い説明, 長い説明)。許可を待っているのはこの中のどれか
    pending: dict[str, tuple[str, str]] = field(default_factory=dict)
    # 最後の会話の行: "end" (返事を終えた) / "tool_use" (道具を呼んだ) / "tool_result" (結果を返した) / "user"
    _last_kind: str = ""

    def waiting_on(self) -> tuple[str, str]:
        """結果を待っている道具 (短い説明, 長い説明)。無ければ ("", "")。並べて呼んだときは最後のもの + ほかの数"""
        if not self.pending:
            return "", ""
        items = list(self.pending.values())
        short, full = items[-1]
        if len(items) > 1:
            short += f"  ほか {len(items) - 1}"
            full = "\n".join(f for _, f in items)
        return short, full

    def to_json(self) -> dict:
        return {"id": self.id, "title": self.title, "last_prompt": self.last_prompt, "cwd": self.cwd,
                "waiting_on": self.waiting_on()[0],
                "branch": self.branch, "updated": self.updated, "state": self.state,
                "edited": sorted(self.edited, key=lambda k: -self.edited[k]), "urls": self.urls}


@dataclass
class Window:
    folder: str  # VS Code で開いているフォルダ
    name: str  # フォルダの名前 (窓の題名の真ん中に出るもの)
    pid: int  # VS Code のプロセス (どの窓も同じ)
    sessions: list[Session] = field(default_factory=list)
    urls: list[tuple[str, str]] = field(default_factory=list)  # 窓の URL: (URL, 名前)。足したもの → LPortMan の台帳の順
    added: set[str] = field(default_factory=set)  # urls のうち、link add で足したもの
    hidden: set[str] = field(default_factory=set)  # 隠す URL (この窓の分と、どの窓でも隠す分)
    git: dict[str, "GitState"] = field(default_factory=dict)  # リポジトリの根 → やり残し (窓のフォルダと、編集したもの)

    def session_urls(self, s: Session, n: int = 3) -> list[str]:
        """セッションの下に出す URL。隠したものと、窓の下に出ているものは出さない"""
        shown = {u for u, _ in self.urls}
        return [u for u in s.urls if u not in self.hidden and u not in shown][:n]

    def to_json(self) -> dict:
        return {"folder": self.folder, "name": self.name, "pid": self.pid,
                "sessions": [s.to_json() | {"urls": self.session_urls(s, 8)} for s in self.sessions],
                "urls": self.urls, "added": sorted(self.added), "hidden": sorted(self.hidden),
                "git": {k: v.__dict__ for k, v in self.git.items()}}


# ── git のやり残し ──
@dataclass
class GitState:
    dirty: int = 0  # 未コミットのファイルの数 (追跡していない新しいファイルも数える)
    ahead: int | None = None  # 未 push のコミットの数。リモートが無ければ None
    upstream: bool = False  # 上流のブランチがある (無ければ ahead はどのリモートにも無いコミットの数)

    def label(self) -> str:
        parts = []
        if self.dirty:
            parts.append(f"未コミット {self.dirty}")
        if self.ahead:
            parts.append(f"未push {self.ahead}")
        return "  ".join(parts)


def _git(root: str, *args: str) -> str | None:
    import subprocess
    try:
        r = subprocess.run(["git", "-C", root, *args], capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=5, stdin=subprocess.DEVNULL,
                           creationflags=0x08000000 if sys.platform == "win32" else 0,  # 黒い窓を出さない
                           env=os.environ | {"GIT_OPTIONAL_LOCKS": "0"})  # 人の git と index.lock を取り合わない
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout if r.returncode == 0 else None  # 持ち主の違うリポジトリ (dubious ownership) などは出さない


def git_state(root: str) -> GitState | None:
    out = _git(root, "status", "--porcelain=v1", "-b")
    if out is None:
        return None
    lines = out.splitlines()
    head = lines[0] if lines and lines[0].startswith("## ") else ""
    st = GitState(dirty=sum(1 for x in lines if x and not x.startswith("## ")))
    if "..." in head:
        st.upstream = True
        m = re.search(r"\[ahead (\d+)", head)
        st.ahead = int(m.group(1)) if m else 0
    elif (_git(root, "remote") or "").strip():
        n = _git(root, "rev-list", "--count", "HEAD", "--not", "--remotes")
        st.ahead = int(n.strip()) if n and n.strip().isdigit() else None
    return st


# ── 足した URL・隠した URL (claude_links.json) ──
# {"folders": {"<フォルダ (normcase)>": {"folder": "<元の書き方>", "add": [{"url", "name", "at"}], "hide": [URL...]}}}
# 隠すときの "*" はどの窓でも隠す。ドックは読むだけ (欄の右クリックで隠すときだけ書く)

def load_links(path: Path = LINKS_JSON) -> dict:
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
        return d if isinstance(d.get("folders"), dict) else {"folders": {}}
    except (OSError, ValueError, AttributeError):
        return {"folders": {}}


def save_links(d: dict, path: Path = LINKS_JSON) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)  # 欄が読みかけの中途半端なファイルを読まない


def _entry(d: dict, folder: str) -> dict:
    key = ALL if folder == ALL else os.path.normcase(os.path.normpath(folder))
    return d["folders"].setdefault(key, {"folder": folder, "add": [], "hide": []})


def _lookup(d: dict, folder: str) -> dict:
    return d["folders"].get(os.path.normcase(os.path.normpath(folder))) or {}


def edit_links(op: str, url: str, folder: str, name: str = "", path: Path = LINKS_JSON) -> str:
    """op: add / remove / hide / unhide。結果を一言で返す"""
    url = norm_url(url)
    if not re.match(r"https?://[^/?#\s]+", url):
        raise ValueError(f"http:// か https:// で始まる URL を渡す: {url}")
    d = load_links(path)
    e = _entry(d, folder)
    e.setdefault("add", [])
    e.setdefault("hide", [])
    where = "どの窓でも" if folder == ALL else Path(folder).name
    if op == "add":
        e["add"] = [a for a in e["add"] if a.get("url") != url]
        e["add"].insert(0, {"url": url, "name": name, "at": int(time.time())})
        if url in e["hide"]:
            e["hide"].remove(url)
        msg = f"{where} に足した: {url}" + (f" ({name})" if name else "")
    elif op == "remove":
        before = len(e["add"])
        e["add"] = [a for a in e["add"] if a.get("url") != url]
        msg = f"{where} から外した: {url}" if len(e["add"]) < before else f"{where} に足した URL ではない: {url}"
    elif op == "hide":
        if url not in e["hide"]:
            e["hide"].append(url)
        e["add"] = [a for a in e["add"] if a.get("url") != url]
        msg = f"{where} で隠した: {url}"
    elif op == "unhide":
        msg = f"{where} で出すようにした: {url}" if url in e["hide"] else f"{where} で隠していない: {url}"
        e["hide"] = [u for u in e["hide"] if u != url]
    else:
        raise ValueError(op)
    if not e["add"] and not e["hide"]:
        d["folders"] = {k: v for k, v in d["folders"].items() if v is not e}
    save_links(d, path)
    return msg


def _iso_to_epoch(ts: str) -> float:
    try:
        return time.mktime(time.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S")) - time.timezone
    except (ValueError, TypeError):
        return 0.0


class Scanner:
    """記録は大きい (数 MB) ので、ファイルごとに読んだ所を覚えて、増えた分だけ読む。"""

    def __init__(self, claude_dir: Path = CLAUDE_DIR, ports_json: Path = PORTS_JSON, links_json: Path = LINKS_JSON):
        self.claude_dir = claude_dir
        self.ports_json = ports_json
        self.links_json = links_json
        self.sessions: dict[str, Session] = {}  # ファイルのパス → Session
        self._git: dict[str, tuple[float, GitState | None]] = {}  # リポジトリの根 (normcase) → (取った時刻, 状態)
        self.git_wanted: set[str] = set()  # 欄に出ているリポジトリの根

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
    def scan(self, now: float | None = None, git: bool = True) -> list[Window]:
        """git=False なら git を呼ばず、覚えている状態だけ付ける (欄は git を別のスレッドで refresh_git する。
        12 のリポジトリで合わせて約 9 秒かかり、同じスレッドでは、その間セッションの状態が止まる)"""
        now = now or time.time()
        wins = self.windows()
        dirs = {p.name.lower(): p for p in (self.claude_dir / "projects").iterdir() if p.is_dir()} \
            if (self.claude_dir / "projects").is_dir() else {}
        registry = self._registry()
        links = load_links(self.links_json)
        hide_all = set((links["folders"].get(ALL) or {}).get("hide") or [])
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
            for root in {git_root(w.folder)} | {r for s in w.sessions for r in s.edited}:
                if root:
                    self.git_wanted.add(root)
                    if git:
                        self._git_state(root, now)
                    got = self._git.get(os.path.normcase(root))
                    if got and got[1]:
                        w.git[root] = got[1]
            mine = _lookup(links, w.folder)
            w.hidden = hide_all | set(mine.get("hide") or [])
            w.urls = []
            for a in mine.get("add") or []:  # Claude に頼んで足したものを先に
                if a.get("url") and a["url"] not in w.hidden:
                    w.urls.append((a["url"], a.get("name", "")))
                    w.added.add(a["url"])
            for r in registry:
                url = f"http://localhost:{r['port']}"
                if (any(same_path(r.get("project", ""), root) for root in roots) and url not in {u for u, _ in w.urls}
                        and url not in w.hidden):
                    w.urls.append((url, r.get("name", "")))
        return wins

    def _git_state(self, root: str, now: float) -> GitState | None:
        key = os.path.normcase(root)
        got = self._git.get(key)
        if not got or now - got[0] >= GIT_SECONDS:
            got = (now, git_state(root))
            self._git[key] = got
        return got[1]

    def refresh_git(self, now: float | None = None) -> None:
        """欄に出ているリポジトリの git の状態を、古くなったものだけ取り直す (別のスレッドから呼ぶ)"""
        now = now or time.time()
        for root in list(self.git_wanted):
            self._git_state(root, now)

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
        elif s._last_kind == "tool_use" and now - mtime >= ASK_SECONDS:
            s.state = "asking"
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
                elif t == "assistant" and bt == "tool_use":
                    if b.get("id"):
                        s.pending[b["id"]] = describe_tool(b.get("name") or "?", b.get("input") or {})
                    if b.get("name") in EDIT_TOOLS:
                        fp = (b.get("input") or {}).get("file_path") or (b.get("input") or {}).get("notebook_path")
                        root = edited_repo(fp) if fp else None
                        if root:
                            s.edited[root] = when
                elif t == "user" and bt == "tool_result":
                    s.pending.pop(b.get("tool_use_id") or "", None)
            if t == "assistant":
                kinds = {b.get("type") for b in blocks if isinstance(b, dict)}
                if "tool_use" in kinds:
                    s._last_kind = "tool_use"
                elif msg.get("stop_reason") == "end_turn" or ("text" in kinds and msg.get("stop_reason") != "tool_use"):
                    s._last_kind = "end"
                    s.pending.clear()  # 返事を終えた (中断で結果の返らなかった呼び出しも、もう待っていない)
            else:
                s._last_kind = "tool_result" if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in blocks) \
                    else "user"
                if s._last_kind == "user":
                    s.pending.clear()  # 人が新しく書いた (中断して書き直した、を含む)

    @staticmethod
    def _urls(s: Session, text: str) -> None:
        for m in URL_RE.finditer(text):
            u = norm_url(m.group(0).rstrip(URL_TRAIL))
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


STATE_LABEL = {"busy": "動作中", "waiting": "返事待ち", "asking": "許可待ちかも", "stalled": "考え中か中断"}


def ago(seconds: float) -> str:
    if seconds < 60:
        return "たった今"  # 秒は出さない (欄を秒ごとに描き直すことになる)
    if seconds < 3600:
        return f"{int(seconds // 60)}分前"
    if seconds < 86400:
        return f"{int(seconds // 3600)}時間前"
    return f"{int(seconds // 86400)}日前"


def window_for(cwd: str, wins: list[Window]) -> str | None:
    """作業フォルダ → それを開いている VS Code の窓のフォルダ (いちばん深く含むもの)。無ければ None"""
    c = os.path.normcase(os.path.normpath(cwd))
    best = None
    for w in wins:
        f = os.path.normcase(os.path.normpath(w.folder))
        if (c == f or c.startswith(f.rstrip(os.sep) + os.sep)) and (best is None or len(f) > len(best[0])):
            best = (f, w.folder)
    return best[1] if best else None


def link_main(argv: list[str], prog: str = "claude_sessions.py link") -> int:
    import argparse
    p = argparse.ArgumentParser(prog=prog,
                                description="WinTabDock の Claude の欄に出す URL を足す・隠す")
    p.add_argument("op", choices=["add", "remove", "hide", "unhide", "list"])
    p.add_argument("url", nargs="?")
    p.add_argument("--name", default="", help="欄に出す名前 (add のとき。例: 本番、プレビュー)")
    p.add_argument("--folder", help="どの窓か (VS Code で開いているフォルダ)。省くと今のフォルダを開いている窓")
    p.add_argument("--all", action="store_true", help="どの窓でも (hide / unhide のとき)")
    a = p.parse_args(argv)
    wins = Scanner().windows()
    if a.all:
        if a.op not in ("hide", "unhide"):
            p.error("--all は hide / unhide だけ")
        folder = ALL
    else:
        given = a.folder or os.getcwd()
        folder = window_for(given, wins)
        if not folder:
            # 窓が開いていなくても控えておく (その窓を開けば出る)
            folder = os.path.normpath(given)
            print(f"注意: {folder} を開いている VS Code の窓が無い。控えておき、開いたら出す", file=sys.stderr)
    if a.op == "list":
        d = load_links()
        e = (d["folders"].get(ALL) or {}) if folder == ALL else _lookup(d, folder)
        print(f"窓: {folder}")
        for x in e.get("add") or []:
            print(f"  足した  {x['url']}" + (f"  ({x['name']})" if x.get("name") else ""))
        for u in e.get("hide") or []:
            print(f"  隠した  {u}")
        for u in (d["folders"].get(ALL) or {}).get("hide") or []:
            print(f"  隠した  {u}  (どの窓でも)")
        return 0
    if not a.url:
        p.error("URL を渡す")
    try:
        print(edit_links(a.op, a.url, folder, a.name))
    except ValueError as e:
        print(e, file=sys.stderr)
        return 2
    return 0


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    if sys.argv[1:2] == ["link"]:
        sys.exit(link_main(sys.argv[2:]))
    wins = Scanner().scan()
    if "--json" in sys.argv:
        print(json.dumps([w.to_json() for w in wins], ensure_ascii=False, indent=1))
        return
    now = time.time()
    for w in wins:
        g = w.git.get(git_root(w.folder) or "")
        print(f"■ {w.name}  ({w.folder})" + (f"  [{g.label()}]" if g and g.label() else ""))
        for url, name in w.urls:
            print(f"    URL {url}  {name}")
        for s in w.sessions:
            print(f"  ● {s.title or s.last_prompt[:30]}  [{STATE_LABEL[s.state]} {ago(now - s.updated)}]"
                  f"{'  ' + s.branch if s.branch else ''}")
            if s.state in ("asking", "stalled") and s.pending:
                print(f"      待っている道具: {s.waiting_on()[0]}")
            others = [r for r in s.edited if not same_path(r, w.folder)]
            for r in others:
                g = w.git.get(r)
                print(f"      編集: {r}" + (f"  [{g.label()}]" if g and g.label() else ""))
            for u in w.session_urls(s):
                print(f"      URL {u}")


if __name__ == "__main__":
    main()
