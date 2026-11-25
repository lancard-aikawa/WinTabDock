"""プロセスの CPU を測り、Claude Code のセッションごと (claude.exe とその子孫) にまとめる。

標準ライブラリ (ctypes) だけ。Tk に触らないので、欄の一覧づくりのスレッドから呼ぶ。

    uv run proc_cpu.py        # 2 秒測って、セッションごとと全体の重いものを文字で出す

- セッションとプロセスの対応は ~/.claude/sessions/<pid>.json (Claude Code が起動中のプロセスごとに書く。
  pid・sessionId・procStart = プロセスの作成時刻 (FILETIME))。作成時刻が違えば、pid を使い回した別のプロセスとして捨てる
- 子孫は親の pid をたどる。親より後に作られたものだけ子とみなす (pid の使い回しで、無関係なものを拾わない)
- 割合は CPU を使った時間で、PC 全体を 100% とする (16 コアなら 1 コアを使い切って約 6%)。タスクマネージャーの
  「詳細」タブと同じ。「プロセス」「パフォーマンス」タブは周波数で割り引いた値 (% Processor Utility) なので低めに出る
  (2026-11-25、i7-12650H で全コアが埋まっていたとき、こちらは 100%、Utility は 26%)
- 見えないもの: 測る間に始まって終わった短いプロセス (コンパイラを次々呼ぶビルドは一部しか乗らない)、
  親が先に終わって親子がたどれなくなったもの、WSL の中 (全部 vmmemWSL に乗る。全体の一覧には出る)。
  コマンドラインは、管理者でないと開けないプロセス (サービス) では読めない (CPU は読める)
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import json
import os
import struct
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

HOME = Path(os.environ.get("USERPROFILE") or Path.home())
SESSIONS_DIR = HOME / ".claude" / "sessions"
NCPU = os.cpu_count() or 1
MIN_INTERVAL = 1.0  # これより短い間で測り直さない (割合がぶれる)

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
ProcessCommandLineInformation = 60


class UNICODE_STRING(ctypes.Structure):
    _fields_ = [("Length", wt.USHORT), ("MaximumLength", wt.USHORT), ("Buffer", ctypes.c_void_p)]


if sys.platform == "win32":
    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _k32.OpenProcess.restype = wt.HANDLE
    _k32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
    _k32.GetSystemTimes.argtypes = [ctypes.POINTER(wt.FILETIME)] * 3
    _k32.CloseHandle.argtypes = [wt.HANDLE]
    _ntdll = ctypes.WinDLL("ntdll")
    _ntdll.NtQuerySystemInformation.argtypes = [ctypes.c_int, ctypes.c_void_p, wt.ULONG, ctypes.POINTER(wt.ULONG)]
    _ntdll.NtQueryInformationProcess.argtypes = [wt.HANDLE, ctypes.c_int, ctypes.c_void_p, wt.ULONG,
                                                 ctypes.POINTER(wt.ULONG)]


def _ft(f: wt.FILETIME) -> int:
    return (f.dwHighDateTime << 32) | f.dwLowDateTime


_buf_size = 1 << 20


def processes() -> list[tuple[int, int, str, int, int]]:
    """(pid, 親の pid, exe の名前, 作成時刻, 使った CPU の時間) の一覧。時刻と時間は 100ns 単位。
    NtQuerySystemInformation(SystemProcessInformation) で一度に読む (タスクマネージャーと同じ)。
    OpenProcess では、管理者でないと Defender (MsMpEng)・検索の索引・vmmemWSL・dwm・svchost が開けず測れない"""
    global _buf_size
    for _ in range(8):
        buf = ctypes.create_string_buffer(_buf_size)
        need = wt.ULONG(0)
        st = _ntdll.NtQuerySystemInformation(5, buf, _buf_size, ctypes.byref(need)) & 0xFFFFFFFF
        if st == 0xC0000004:  # STATUS_INFO_LENGTH_MISMATCH
            _buf_size = max(_buf_size * 2, need.value + 65536)
            continue
        if st != 0:
            return []
        break
    else:
        return []
    base = ctypes.addressof(buf)
    raw = buf.raw
    out = []
    off = 0
    while True:
        nxt = struct.unpack_from("<I", raw, off)[0]
        created, user, kernel = struct.unpack_from("<qqq", raw, off + 32)
        nlen, _, ptr = struct.unpack_from("<HH4xQ", raw, off + 56)
        pid, ppid = struct.unpack_from("<QQ", raw, off + 80)
        if ptr and nlen:
            s = ptr - base
            name = raw[s:s + nlen].decode("utf-16-le", "replace")
        else:
            name = "System Idle Process" if pid == 0 else "?"
        out.append((pid, ppid, name, created, user + kernel))
        if not nxt:
            break
        off += nxt
    return out


def command_line(pid: int) -> str:
    """プロセスのコマンドライン。読めなければ ""。重いものの説明に使う (毎回すべては読まない)"""
    h = _k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return ""
    try:
        need = wt.ULONG(0)
        _ntdll.NtQueryInformationProcess(h, ProcessCommandLineInformation, None, 0, ctypes.byref(need))
        if not need.value:
            return ""
        buf = ctypes.create_string_buffer(need.value)
        if _ntdll.NtQueryInformationProcess(h, ProcessCommandLineInformation, buf, need, ctypes.byref(need)) != 0:
            return ""
        us = UNICODE_STRING.from_buffer(buf)
        return ctypes.wstring_at(us.Buffer, us.Length // 2) if us.Buffer else ""
    finally:
        _k32.CloseHandle(h)


def system_busy() -> tuple[int, int]:
    """(使った時間, 全部の時間)。GetSystemTimes の kernel は idle を含む"""
    i, k, u = wt.FILETIME(), wt.FILETIME(), wt.FILETIME()
    _k32.GetSystemTimes(ctypes.byref(i), ctypes.byref(k), ctypes.byref(u))
    total = _ft(k) + _ft(u)
    return total - _ft(i), total


@dataclass
class Proc:
    pid: int
    name: str
    cpu: float  # PC 全体を 100 とした割合
    cmd: str = ""


@dataclass
class SessionCpu:
    session_id: str
    pid: int  # claude.exe
    cpu: float = 0.0  # claude.exe と子孫の合計
    procs: list[Proc] = field(default_factory=list)  # 重い順 (claude.exe 自身も入る)

    def top(self) -> Proc | None:
        """欄に名前を出すもの: 子孫の中で一番重いもの。子孫が使っていなければ claude.exe"""
        kids = [p for p in self.procs if p.pid != self.pid and p.cpu > 0]
        return kids[0] if kids else (self.procs[0] if self.procs else None)


@dataclass
class Snapshot:
    at: float = 0.0
    interval: float = 0.0  # 何秒の間の平均か。0 ならまだ測れていない (1 回目)
    total: float = 0.0  # PC 全体の使用率
    sessions: dict[str, SessionCpu] = field(default_factory=dict)  # sessionId → 使用量
    top: list[tuple[str, float, str]] = field(default_factory=list)  # 全体の重いもの (名前, 割合, 何か)。重い順

    def to_json(self) -> dict:
        return {"interval": round(self.interval, 2), "total": round(self.total, 1),
                "sessions": {k: {"pid": v.pid, "cpu": round(v.cpu, 1),
                                 "procs": [p.__dict__ | {"cpu": round(p.cpu, 1)} for p in v.procs[:5]]}
                             for k, v in self.sessions.items()},
                "top": [[n, round(c, 1), w] for n, c, w in self.top]}


def claude_processes(sessions_dir: Path = SESSIONS_DIR) -> dict[int, tuple[str, int]]:
    """claude.exe の pid → (sessionId, 作成時刻)"""
    out = {}
    try:
        files = list(sessions_dir.glob("*.json"))
    except OSError:
        return out
    for f in files:
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
            out[int(d["pid"])] = (str(d["sessionId"]), int(d.get("procStart") or 0))
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return out


class CpuSampler:
    """sample() を呼ぶたびに、前に呼んだときからの CPU の使い方を返す。1 回目は割合が出ない (interval=0)"""

    def __init__(self, sessions_dir: Path = SESSIONS_DIR):
        self.sessions_dir = sessions_dir
        self._prev: dict[int, tuple[int, int]] = {}  # pid → (作成時刻, CPU の時間)
        self._prev_sys: tuple[int, int] | None = None
        self._prev_at = 0.0
        self._cmd: dict[tuple[int, int], str] = {}  # (pid, 作成時刻) → コマンドライン (1 度だけ読む)
        self.last = Snapshot()

    def sample(self, now: float | None = None) -> Snapshot:
        now = now or time.time()
        if self._prev_at and now - self._prev_at < MIN_INTERVAL:
            return self.last
        procs = [p for p in processes() if p[0]]  # pid 0 (System Idle Process) は使っていない時間
        cur = {pid: (created, used) for pid, _, _, created, used in procs}
        sys_now = system_busy()
        interval = now - self._prev_at if self._prev_at else 0.0
        snap = Snapshot(at=now, interval=interval)
        if interval and self._prev_sys:
            busy, total = sys_now[0] - self._prev_sys[0], sys_now[1] - self._prev_sys[1]
            snap.total = 100.0 * busy / total if total > 0 else 0.0
            wall = interval * 1e7 * NCPU  # 100ns 単位の、全部のコアの時間
            pct: dict[int, float] = {}
            for pid, (created, used) in cur.items():
                before = self._prev.get(pid)
                if before and before[0] == created:
                    d = used - before[1]
                elif created >= (self._prev_at - 0.05) * 1e7 + 116444736000000000:  # 測る間に始まった
                    d = used
                else:
                    continue
                if d > 0:
                    pct[pid] = min(100.0, 100.0 * d / wall)
            self._group(snap, procs, cur, pct)
        self._prev, self._prev_sys, self._prev_at = cur, sys_now, now
        # 終わったプロセスのコマンドラインを捨てる
        self._cmd = {k: v for k, v in self._cmd.items() if cur.get(k[0], (None,))[0] == k[1]}
        self.last = snap
        return snap

    def _cmdline(self, pid: int, created: int) -> str:
        key = (pid, created)
        if key not in self._cmd:
            self._cmd[key] = command_line(pid)
        return self._cmd[key]

    def _group(self, snap: Snapshot, procs: list[tuple[int, int, str, int, int]], cur: dict[int, tuple[int, int]],
               pct: dict[int, float]) -> None:
        names = {p[0]: p[2] for p in procs}
        children: dict[int, list[int]] = {}
        for pid, ppid, *_ in procs:
            # 親より後に作られたものだけ子とみなす (親の pid が使い回されていると、無関係なものが子に見える)
            if pid and ppid and pid != ppid and pid in cur and ppid in cur and cur[pid][0] >= cur[ppid][0]:
                children.setdefault(ppid, []).append(pid)
        owner: dict[int, str] = {}  # pid → sessionId
        for root, (sid, started) in claude_processes(self.sessions_dir).items():
            if root not in cur or (started and cur[root][0] != started):
                continue  # もう無い、か pid を使い回した別のプロセス
            sc = SessionCpu(session_id=sid, pid=root)
            stack = [root]
            while stack:
                p = stack.pop()
                if p in owner:
                    continue
                owner[p] = sid
                if pct.get(p):
                    sc.procs.append(Proc(pid=p, name=names.get(p, "?"), cpu=pct[p]))
                stack.extend(children.get(p, []))
            sc.cpu = sum(x.cpu for x in sc.procs)
            sc.procs.sort(key=lambda x: -x.cpu)
            for x in sc.procs[:3]:
                x.cmd = self._cmdline(x.pid, cur[x.pid][0])
            snap.sessions[sid] = sc
        # 全体: セッションは 1 つにまとめ、外は exe の名前でまとめる (chrome.exe は何十にも分かれる)
        groups: dict[str, list] = {}
        for pid, c in pct.items():
            if pid in owner:
                key, what = "s:" + owner[pid], "session"
            else:
                key, what = "x:" + names.get(pid, "?").lower(), names.get(pid, "?")
            g = groups.setdefault(key, [what, 0.0])
            g[1] += c
        snap.top = sorted(((k[2:], c, w) for k, (w, c) in groups.items()), key=lambda t: -t[1])[:8]


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    s = CpuSampler()
    s.sample()
    time.sleep(2.0)
    snap = s.sample()
    if "--json" in sys.argv:
        print(json.dumps(snap.to_json(), ensure_ascii=False, indent=1))
        return
    print(f"全体 {snap.total:.0f}%  ({snap.interval:.1f} 秒の平均、{NCPU} コア)")
    for name, c, what in snap.top:
        print(f"  {c:5.1f}%  {'セッション ' + name if what == 'session' else what}")
    for sid, sc in sorted(snap.sessions.items(), key=lambda kv: -kv[1].cpu):
        print(f"■ {sid}  pid {sc.pid}  {sc.cpu:.1f}%")
        for p in sc.procs[:5]:
            print(f"    {p.cpu:5.1f}%  {p.name} ({p.pid})  {p.cmd[:100]}")


if __name__ == "__main__":
    main()
