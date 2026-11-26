"""ドックの左に出す「Claude」の欄。開いている VS Code の窓ごとに、中の Claude Code のセッションと見る URL を並べる。

- 窓の行を押すと、その VS Code の窓をタブにして (取り込み済みならそのタブへ) 切り替える
- セッションの行を押しても、その窓へ切り替える (VS Code の中のどのチャットのタブかまでは外から選べない)
- URL を押すと既定のブラウザで開く。右クリックで隠す (claude_links.json に書く。Claude からは claude_sessions.py link で足す・隠す)

一覧づくり (claude_sessions.Scanner) は別のスレッドで回し、Tk はこのスレッドからだけ触る。
"""
from __future__ import annotations

import ctypes
import os
import queue
import re
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
import webbrowser
from typing import TYPE_CHECKING, Callable

import claude_sessions as cs
import proc_cpu

if TYPE_CHECKING:
    from wintabdock import Dock

SCAN_SECONDS = 3.0
PANEL_W = 290

BG = "#141428"
CARD_BG = "#1a1a2e"
WIN_BG, WIN_HOVER, WIN_ACTIVE = "#2e2e52", "#3b3b68", "#7c3aed"
ROW_BG, ROW_HOVER = "#20203a", "#2c2c4c"
LINK_FG, LINK_EDGE, LINK_HOVER = "#9cc3ff", "#3d5a8a", "#22304a"
FG, FG_DIM = "#ffffff", "#9a9ab8"
# 知らせ (動いていたセッションが、こちらを待つようになった)。窓の行とセッションの行を橙で塗る
ATTN_BG, ATTN_HOVER, ROW_ATTN, ROW_ATTN_HOVER = "#b45309", "#c2650f", "#5c3a12", "#6e4818"
GIT_FG = "#fdba74"  # やり残しの札 (押せない)
STATE_STYLE = {  # 押せない札: 色の点と文字だけ (塗りも枠も付けない)
    "busy": ("●", "#4ade80", "動作中"),
    "waiting": ("●", "#facc15", "返事待ち"),
    "asking": ("●", "#fb923c", "許可待ちかも"),
    "stalled": ("◌", "#9a9ab8", "考え中か中断"),
}
WANTS_YOU = ("waiting", "asking")  # この状態に変わったら知らせる
# CPU (PC 全体を 100%)。セッションの行には CPU_SHOW 以上のときだけ出し、CPU_HEAVY 以上は赤くする (16 コアで約 4 コア分)
CPU_SHOW, CPU_HEAVY = 3.0, 25.0
CPU_FG, CPU_HEAVY_FG = "#67e8f9", "#f87171"


def vscode_title_matches(title: str, name: str) -> bool:
    """VS Code の窓の題名は「前のタブ - フォルダ名 - Visual Studio Code」か「フォルダ名 - Visual Studio Code」"""
    return re.search(rf"(^|\s-\s){re.escape(name)}\s-\sVisual Studio Code", title) is not None


class Tooltip:
    def __init__(self, widget: tk.Widget, text: str):
        self.widget, self.text, self.tip = widget, text, None
        widget.bind("<Enter>", self.show, add="+")
        widget.bind("<Leave>", self.hide, add="+")

    def show(self, _e=None) -> None:
        if self.tip or not self.text:
            return
        self.tip = tk.Toplevel(self.widget)
        self.tip.wm_overrideredirect(True)
        x = self.widget.winfo_rootx() + 12
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 2
        self.tip.wm_geometry(f"+{x}+{y}")
        tk.Label(self.tip, text=self.text, bg="#ffffe0", fg="#000000", relief=tk.SOLID, bd=1, justify=tk.LEFT,
                 wraplength=420, padx=6, pady=3).pack()

    def hide(self, _e=None) -> None:
        if self.tip:
            self.tip.destroy()
            self.tip = None


def hover(widget: tk.Widget, normal: str, over: str) -> None:
    widget.bind("<Enter>", lambda e: widget.configure(bg=over), add="+")
    widget.bind("<Leave>", lambda e: widget.configure(bg=normal), add="+")


class ClaudePanel:
    def __init__(self, dock: "Dock", parent: tk.Misc, title_of: Callable[[int], str],
                 scanner: cs.Scanner | None = None):
        self.dock = dock
        self.title_of = title_of  # 窓の題名 (wintabdock.window_title)
        self.scanner = scanner or cs.Scanner()
        self.frame = tk.Frame(parent, bg=BG, width=PANEL_W)
        self.frame.pack_propagate(False)
        family = tkfont.nametofont("TkDefaultFont").actual("family")
        self.font = tkfont.Font(family=family, size=9)
        self.font_bold = tkfont.Font(family=family, size=10, weight="bold")
        self.font_small = tkfont.Font(family=family, size=8)
        head = tk.Frame(self.frame, bg=BG)
        head.pack(side=tk.TOP, fill=tk.X, padx=8, pady=(6, 2))
        tk.Label(head, text="Claude", bg=BG, fg=FG, font=self.font_bold).pack(side=tk.LEFT)  # 見出し (押せない)
        self.status = tk.Label(head, text="読み込み中…", bg=BG, fg=FG_DIM, font=self.font_small)
        self.status.pack(side=tk.RIGHT)
        # PC 全体の CPU (押せない札)。乗せると重いものの上位 (Claude の外も) を出す
        self.cpu_label = tk.Label(head, text="", bg=BG, fg=FG_DIM, font=self.font_small)
        self.cpu_label.pack(side=tk.RIGHT, padx=(0, 6))
        self.cpu_tip = Tooltip(self.cpu_label, "")
        self.sampler = proc_cpu.CpuSampler()
        self.cpu = proc_cpu.Snapshot()
        # 中身は縦に長くなるので、Canvas に載せて巻き取れるようにする
        self.canvas = tk.Canvas(self.frame, bg=BG, highlightthickness=0, bd=0)
        self.scroll = tk.Scrollbar(self.frame, orient=tk.VERTICAL, command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.scroll.set)
        self.scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.inner = tk.Frame(self.canvas, bg=BG)
        self._inner_id = self.canvas.create_window(0, 0, window=self.inner, anchor=tk.NW)
        self.inner.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox(tk.ALL)))
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(self._inner_id, width=e.width))
        self.frame.bind_all("<MouseWheel>", self._wheel, add="+")
        self.windows: list[cs.Window] = []
        # 窓のフォルダ → {"frame", "head", "head_key", "body", "rows": {セッション: (行, 鍵)}, "order"}。鍵が変わった所だけ作り直す
        self._cards: dict[str, dict] = {}
        self._card_order: list[str] = []
        self._empty: tk.Label | None = None
        self._attn_drawn = None
        self._active: str | None = None
        self._order: dict[str, list[str]] = {}  # 窓 → セッションの並び
        self._win_buttons = {}
        self._state_labels = {}
        self._title_buttons: dict[tuple[str, str], tk.Button] = {}
        self._tool_labels = {}
        self._cpu_labels = {}
        self._prev: dict[tuple[str, str], str] = {}  # (窓, セッション) → 前に見た状態
        self.attention: set[tuple[str, str]] = set()  # 知らせているセッション。その窓を見たら消す
        self._results: queue.SimpleQueue = queue.SimpleQueue()
        self._stop = threading.Event()
        self._wake = threading.Event()  # 欄から URL を隠したときに、すぐ取り直させる
        self.visible = False
        threading.Thread(target=self._scan_loop, daemon=True).start()
        threading.Thread(target=self._git_loop, daemon=True).start()
        threading.Thread(target=self._cpu_loop, daemon=True).start()
        self.frame.after(300, self._drain)

    # ── 表示・非表示 ──
    def show(self, before: tk.Widget) -> None:
        self.frame.pack(side=tk.LEFT, fill=tk.Y, before=before)
        self.visible = True

    def hide(self) -> None:
        self.frame.pack_forget()
        self.visible = False

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def _wheel(self, e: tk.Event) -> None:
        w = e.widget
        while w is not None:  # 欄の上でだけ巻き取る
            if w is self.frame:
                self.canvas.yview_scroll(-1 if e.delta > 0 else 1, "units")
                return
            w = getattr(w, "master", None)

    # ── 一覧づくり (別のスレッド) ──
    def _scan_loop(self) -> None:
        while not self._stop.is_set():
            try:
                wins = self.scanner.scan(git=False)
                self._results.put(("ok", wins, time.time()))
            except Exception as e:  # 読めないファイルなどで止めない
                self._results.put(("error", repr(e), time.time()))
            self._wake.wait(SCAN_SECONDS)
            self._wake.clear()

    def _cpu_loop(self) -> None:
        """CPU は一覧づくりと別に測る (一覧づくりは初回に記録を全部読むので数十秒かかることがあり、重いときほど遅れる)"""
        while not self._stop.is_set():
            try:
                self._results.put(("cpu", self.sampler.sample(), time.time()))
            except Exception:
                pass
            self._stop.wait(SCAN_SECONDS)

    def _git_loop(self) -> None:
        """git の状態は別に取る (遅い。一覧づくりを止めない)。取れたら一覧を取り直させる"""
        while not self._stop.is_set():
            if self.scanner.git_wanted:
                try:
                    self.scanner.refresh_git()
                except Exception:
                    pass
                self._wake.set()
            self._stop.wait(cs.GIT_SECONDS if self.scanner.git_wanted else 1.0)

    # ── 知らせ ──
    def looking_at(self, w: cs.Window) -> bool:
        """人がいまその窓を見ているか (ドックでそのタブを選んでいて、ドックかその窓が前面。ドックの外の窓なら、それが前面)"""
        fg = ctypes.windll.user32.GetForegroundWindow() or 0
        tab = self.dock.active
        if tab and fg in (self.dock.hwnd, tab.hwnd) and vscode_title_matches(self.title_of(tab.hwnd), w.name):
            return True
        return bool(fg) and fg != self.dock.hwnd and vscode_title_matches(self.title_of(fg), w.name)

    def update_attention(self) -> None:
        fresh = False
        alive = set()
        for w in self.windows:
            looking = self.looking_at(w)
            for x in w.sessions:
                key = (w.folder, x.id)
                alive.add(key)
                before = self._prev.get(key)
                self._prev[key] = x.state
                if x.state not in WANTS_YOU:
                    self.attention.discard(key)  # また動き出した
                elif before is not None and before not in WANTS_YOU and not looking:
                    if key not in self.attention:
                        fresh = True
                    self.attention.add(key)
            if looking:
                self.attention -= {k for k in self.attention if k[0] == w.folder}
        self.attention &= alive
        names = {w.name for w in self.windows if any(k[0] == w.folder for k in self.attention)}
        self.dock.set_attention({t.hwnd for t in self.dock.tabs
                                 if any(vscode_title_matches(self.title_of(t.hwnd), n) for n in names)})
        if fresh:
            self.dock.flash_taskbar()

    def _drain(self) -> None:
        latest = None
        while not self._results.empty():
            got = self._results.get()
            if got[0] == "cpu":
                self.cpu = got[1]
            else:
                latest = got
        if latest:
            kind, data, _ = latest
            if kind == "ok":
                self.windows = data
                self.status.configure(text="")
                self.dock.adopt_vscode_windows(data)  # 欄を出していなくても取り込む
                self._have_scan = True
            else:
                self.status.configure(text="読めない記録あり")
        if getattr(self, "_have_scan", False):
            self.update_attention()  # 窓を見たら消すので、届いたときだけでなく毎回
        if self.visible:
            self.render()
        self.frame.after(500, self._drain)

    # ── 描く ──
    def active_folder(self) -> str | None:
        tab = self.dock.active
        if not tab:
            return None
        title = self.title_of(tab.hwnd)
        for w in self.windows:
            if vscode_title_matches(title, w.name):
                return w.folder
        return None

    def ordered(self, w: cs.Window) -> list[cs.Session]:
        """並びは、セッションが増えたときだけ変える (動くたびに新しい順へ並べ直すと、行が入れ替わってちらつく)。
        新しく出たセッションは上に足し、消えたものは抜く"""
        by_id = {x.id: x for x in w.sessions}
        old = [i for i in self._order.get(w.folder, []) if i in by_id]
        fresh = [x.id for x in w.sessions if x.id not in old]  # w.sessions は新しい順
        self._order[w.folder] = fresh + old
        return [by_id[i] for i in self._order[w.folder]]

    def render(self) -> None:
        """変わった所だけ作り直し、状態・時刻・選択中の色・CPU は書き換えるだけにする。
        - 3 秒ごとに全部作り直していたときは、「たった今」と「1分前」が行き来するたびに欄が消えて描き直され、ちらついた
          (動いているセッションは書き込むたびに「たった今」に戻る。1 分で 6 回作り直していた)
        - 形が変わったら全部作り直していたときは、どこか 1 つの窓の未コミットの数・会話に出た URL・編集先が変わるたびに、
          欄が丸ごと描き直された (2026-11-25)。いまは窓の見出し (窓の行・やり残し・URL) とセッションの行を別々に作り直す。
          作り直す行は、新しい行を古い行の前に置いてから古い行を消す (間に何も無い瞬間を作らない)"""
        now = time.time()
        rows = [(w, self.ordered(w)) for w in self.windows]
        if self._sync(rows):
            self._active = self._attn_drawn = None  # 新しい部品の色を塗らせる
        # 書き換えるだけのもの
        active = self.active_folder()
        attn = frozenset(self.attention)
        if active != self._active or attn != self._attn_drawn:
            self._active, self._attn_drawn = active, attn
            for w, _ in rows:
                b = self._win_buttons.get(w.folder)
                if not b:
                    continue
                n = sum(1 for k in attn if k[0] == w.folder)
                b.configure(bg=self._win_color(w.folder), text=f"▣ {w.name}" + (f"   待ち {n}" if n else ""))
            for key, b in self._title_buttons.items():
                b.configure(bg=ROW_ATTN if key in attn else ROW_BG)
        for w, ss in rows:
            for x in ss:
                labels = self._state_labels.get((w.folder, x.id))
                if not labels:
                    continue
                mark, color, label = STATE_STYLE.get(x.state, STATE_STYLE["stalled"])
                text = f"{label}  {cs.ago(now - x.updated)}" + (f"  {x.branch}" if x.branch else "")
                if labels[0].cget("text") != mark or labels[0].cget("fg") != color:
                    labels[0].configure(text=mark, fg=color)
                if labels[1].cget("text") != text:
                    labels[1].configure(text=text)
                self._show_tool(w.folder, x)
                self._show_cpu(w.folder, x)
        self._show_total_cpu()

    # ── CPU ──
    def cpu_text(self, sc: proc_cpu.SessionCpu | None) -> tuple[str, str]:
        """セッションの CPU → (欄に出す文字, 吹き出し)。CPU_SHOW 未満なら ("", "")"""
        if not self.cpu.interval or not sc or sc.cpu < CPU_SHOW:
            return "", ""
        top = sc.top()
        busy = [p for p in sc.procs if p.cpu >= 0.5]
        same = sum(1 for p in busy if top and p.name == top.name)
        name = (top.name + (f" ×{same}" if same > 1 else "")) if top else ""
        text = f"⚙ CPU {sc.cpu:.0f}%  {name}"
        tip = [f"CPU {sc.cpu:.0f}% (PC 全体を 100%、{proc_cpu.NCPU} コア。{self.cpu.interval:.0f} 秒の平均)",
               "この Claude のセッションと、それが起動したもの (テスト・ビルド・サーバ) の合計"]
        for p in sc.procs[:6]:
            cmd = " ".join(p.cmd.split())  # python -c の複数行などは 1 行にまとめる
            tip.append(f"{p.cpu:5.1f}%  {p.name}" + (f"\n        {cmd[:140]}" if cmd else ""))
        return text, "\n".join(tip)

    def _show_cpu(self, folder: str, x: cs.Session) -> None:
        """動いているものがあるときだけ、状態の札の下に「⚙ CPU 18%  node.exe ×3」を出す (書き換えるだけ)"""
        got = self._cpu_labels.get((folder, x.id))
        if not got:
            return
        label, tip, state = got
        sc = self.cpu.sessions.get(x.id)
        text, full = self.cpu_text(sc)
        tip.text = full
        fg = CPU_HEAVY_FG if sc and sc.cpu >= CPU_HEAVY else CPU_FG
        if label.cget("text") == text and label.cget("fg") == fg:
            return
        label.configure(text=text, fg=fg)
        if text and not label.winfo_manager():
            label.pack(fill=tk.X, padx=(6, 0), after=state)
        elif not text:
            label.pack_forget()

    def _owner_name(self, sid: str) -> str:
        for w in self.windows:
            for x in w.sessions:
                if x.id == sid:
                    return f"Claude {w.name}: {(x.title or x.last_prompt)[:30]}"
        return f"Claude (欄に無いセッション {sid[:8]})"

    def _show_total_cpu(self) -> None:
        snap = self.cpu
        if not snap.interval:
            return
        text = f"CPU {snap.total:.0f}%"
        if self.cpu_label.cget("text") != text:
            self.cpu_label.configure(text=text, fg=CPU_HEAVY_FG if snap.total >= 60 else FG_DIM)
        lines = [f"PC 全体 {snap.total:.0f}% ({proc_cpu.NCPU} コア。{snap.interval:.0f} 秒の平均)",
                 "CPU を使った時間の割合 (タスクマネージャーの「詳細」タブと同じ。「プロセス」タブより高めに出る)",
                 "重いもの:"]
        for name, c, what in snap.top:
            if c < 0.5:
                break
            lines.append(f"{c:5.1f}%  {self._owner_name(name) if what == 'session' else what}")
        self.cpu_tip.text = "\n".join(lines)

    # ── 作り直す範囲を絞る ──
    @staticmethod
    def _head_key(w: cs.Window) -> tuple:
        g = w.git.get(cs.git_root(w.folder) or "")
        return w.name, tuple(w.urls), tuple(sorted(w.added)), g.label() if g else ""

    @staticmethod
    def _row_key(w: cs.Window, x: cs.Session) -> tuple:
        edited = tuple((r, w.git[r].label() if r in w.git else "") for r in sorted(x.edited)
                       if not cs.same_path(r, w.folder))
        return x.title or x.last_prompt[:40] or x.id[:8], x.last_prompt[:120], x.cwd, edited, tuple(w.session_urls(x))

    def _forget(self, pred: Callable[[tuple[str, str]], bool]) -> None:
        for reg in (self._state_labels, self._title_buttons, self._tool_labels, self._cpu_labels):
            for k in [k for k in reg if pred(k)]:
                del reg[k]

    def _sync(self, rows: list[tuple[cs.Window, list[cs.Session]]]) -> bool:
        """欄の部品を一覧に合わせる。作った部品があれば True"""
        made = False
        if not rows and not self._empty:
            self._empty = tk.Label(self.inner, text="Claude の入った VS Code の窓が\n開いていません", bg=BG, fg=FG_DIM,
                                   font=self.font, justify=tk.LEFT)
            self._empty.pack(anchor=tk.W, padx=10, pady=10)
        elif rows and self._empty:
            self._empty.destroy()
            self._empty = None
        alive = {w.folder for w, _ in rows}
        for folder in [f for f in self._cards if f not in alive]:  # 閉じた窓
            self._cards.pop(folder)["frame"].destroy()
            self._win_buttons.pop(folder, None)
            self._forget(lambda k: k[0] == folder)
        for w, ss in rows:
            c = self._cards.get(w.folder)
            if c is None:
                frame = tk.Frame(self.inner, bg=CARD_BG)
                body = tk.Frame(frame, bg=CARD_BG)
                c = self._cards[w.folder] = {"frame": frame, "head": None, "head_key": None, "body": body,
                                             "rows": {}, "order": []}
            hk = self._head_key(w)
            if hk != c["head_key"]:
                head = tk.Frame(c["frame"], bg=CARD_BG)
                self._window_head(head, w)
                self._swap(c["head"], head, before=c["body"])
                c["head"], c["head_key"] = head, hk
                made = True
            # セッションの行
            ids = [x.id for x in ss]
            for sid in [i for i in c["rows"] if i not in ids]:
                c["rows"].pop(sid)[0].destroy()
                self._forget(lambda k, sid=sid: k == (w.folder, sid))
            for x in ss:
                rk = self._row_key(w, x)
                old = c["rows"].get(x.id)
                if old and old[1] == rk:
                    continue
                row = tk.Frame(c["body"], bg=CARD_BG)
                self._session_row(row, w, x)
                if old:
                    self._swap(old[0], row)
                c["rows"][x.id] = (row, rk)
                made = True
            if ids != c["order"]:  # 並びが変わった・行が増えた (ほとんど無い)
                for sid in ids:
                    c["rows"][sid][0].pack_forget()
                for sid in ids:
                    c["rows"][sid][0].pack(fill=tk.X, padx=(10, 0), pady=(3, 0))
                c["order"] = ids
        order = [w.folder for w, _ in rows]
        if order != self._card_order:  # 窓が開いた・閉じた
            for f in order:
                self._cards[f]["frame"].pack_forget()
            for f in order:
                self._cards[f]["frame"].pack(fill=tk.X, padx=6, pady=(4, 2))
            self._card_order = order
        return made

    @staticmethod
    def _swap(old: tk.Widget | None, new: tk.Widget, before: tk.Widget | None = None) -> None:
        """new を old の場所に置いて old を消す。old が無ければ before の前 (無ければ末尾) に置く"""
        info = dict(old.pack_info()) if old is not None and old.winfo_manager() else None
        if info:
            info.pop("in", None)
            new.pack(**info, before=old)
            old.destroy()
            return
        if old is not None:
            old.destroy()
        if before is not None and before.winfo_manager():
            new.pack(fill=tk.X, before=before)
        else:
            new.pack(fill=tk.X)
            if before is not None:
                before.pack(fill=tk.X)

    def _window_head(self, card: tk.Frame, w: cs.Window) -> None:
        b = tk.Button(card, text=f"▣ {w.name}", anchor=tk.W, bg=WIN_BG, fg=FG, activebackground=WIN_ACTIVE,
                      activeforeground=FG, relief=tk.FLAT, bd=0, padx=8, pady=4, font=self.font_bold, cursor="hand2",
                      command=lambda w=w: self.dock.focus_vscode(w.folder, w.name))
        b.pack(fill=tk.X)
        # 乗せたときの色。選択中か・知らせ中かは後から変わるので、離したときはその時の色に戻す
        b.bind("<Enter>", lambda e, f=w.folder: b.configure(bg=self._win_color(f, hover=True)), add="+")
        b.bind("<Leave>", lambda e, f=w.folder: b.configure(bg=self._win_color(f)), add="+")
        self._win_buttons[w.folder] = b
        Tooltip(b, f"{w.folder}\nこの VS Code の窓へ切り替える")
        g = w.git.get(cs.git_root(w.folder) or "")
        if g and g.label():  # やり残しの札 (押せない)
            tk.Label(card, text=g.label(), bg=CARD_BG, fg=GIT_FG, font=self.font_small, anchor=tk.W).pack(
                fill=tk.X, padx=(10, 0))
        for url, name in w.urls:
            self._link(card, w, url, name, indent=10, added=url in w.added)

    def _show_tool(self, folder: str, x: cs.Session) -> None:
        """動作中でなく、道具の結果を待っているときだけ、状態の札の下に「▸ Bash: npm test」を出す
        (動作中は道具が次々変わるので出さない。書き換えるだけで、欄は作り直さない)"""
        got = self._tool_labels.get((folder, x.id))
        if not got:
            return
        label, tip, state = got
        short, full = x.waiting_on() if x.state in ("asking", "stalled") else ("", "")
        text = f"▸ {short}" if short else ""
        if label.cget("text") == text:
            return
        label.configure(text=text)
        tip.text = full
        if text and not label.winfo_manager():
            label.pack(fill=tk.X, padx=(6, 0), after=state)
        elif not text:
            label.pack_forget()

    def _win_color(self, folder: str, hover: bool = False) -> str:
        if folder == self._active:
            return WIN_ACTIVE
        if any(k[0] == folder for k in self.attention):
            return ATTN_HOVER if hover else ATTN_BG
        return WIN_HOVER if hover else WIN_BG

    def _session_row(self, row: tk.Frame, w: cs.Window, s: cs.Session) -> None:
        """セッションの行の中身を row に作る (row を置くのは _sync)"""
        state =tk.Frame(row, bg=CARD_BG)  # 状態の札 (押せない。塗りも枠も付けない)。文字と色は render が書き換える
        state.pack(fill=tk.X)
        mark = tk.Label(state, text="", bg=CARD_BG, fg=FG_DIM, font=self.font)
        mark.pack(side=tk.LEFT)
        text = tk.Label(state, text="", bg=CARD_BG, fg=FG_DIM, font=self.font_small)
        text.pack(side=tk.LEFT)
        self._state_labels[(w.folder, s.id)] = (mark, text)
        # 結果を待っている道具 (許可待ちかもの中身)。押せない札。出すかどうかと文字は render が書き換える
        tool = tk.Label(row, text="", bg=CARD_BG, fg=STATE_STYLE["asking"][1], font=self.font_small, anchor=tk.W,
                        justify=tk.LEFT, wraplength=PANEL_W - 50)
        tool_tip = Tooltip(tool, "")
        self._tool_labels[(w.folder, s.id)] = (tool, tool_tip, state)
        # CPU (押せない札)。出すかどうかと文字は render が書き換える
        cpu = tk.Label(row, text="", bg=CARD_BG, fg=CPU_FG, font=self.font_small, anchor=tk.W)
        self._cpu_labels[(w.folder, s.id)] = (cpu, Tooltip(cpu, ""), state)
        title = s.title or s.last_prompt[:40] or s.id[:8]
        b = tk.Button(row, text=title, anchor=tk.W, justify=tk.LEFT, wraplength=PANEL_W - 50, bg=ROW_BG, fg=FG,
                      activebackground=ROW_HOVER, activeforeground=FG, relief=tk.FLAT, bd=0, padx=6, pady=2,
                      font=self.font, cursor="hand2", command=lambda: self.dock.focus_vscode(w.folder, w.name))
        b.pack(fill=tk.X)
        key = (w.folder, s.id)
        self._title_buttons[key] = b
        b.bind("<Enter>", lambda e: b.configure(bg=ROW_ATTN_HOVER if key in self.attention else ROW_HOVER), add="+")
        b.bind("<Leave>", lambda e: b.configure(bg=ROW_ATTN if key in self.attention else ROW_BG), add="+")
        tip = [title]
        if s.last_prompt:
            tip.append(f"最後の依頼: {s.last_prompt[:120]}")
        if s.cwd and not cs.same_path(s.cwd, w.folder):
            tip.append(f"作業フォルダ: {s.cwd}")
        tip.append("この VS Code の窓へ切り替える (チャットのタブは VS Code の中で選ぶ)")
        Tooltip(b, "\n".join(tip))
        # 開いたフォルダの外で編集したリポジトリ (claude から始めて別のリポジトリを触っている、を忘れないため)
        for root in sorted(s.edited):
            if cs.same_path(root, w.folder):
                continue
            g = w.git.get(root)
            line = tk.Frame(row, bg=CARD_BG)
            line.pack(fill=tk.X, padx=(6, 0))
            tk.Label(line, text=f"✎ {os.path.basename(root)}", bg=CARD_BG, fg="#c4b5fd", font=self.font_small).pack(
                side=tk.LEFT)
            if g and g.label():
                tk.Label(line, text=f"  {g.label()}", bg=CARD_BG, fg=GIT_FG, font=self.font_small).pack(side=tk.LEFT)
        for url in w.session_urls(s):
            self._link(row, w, url, "", indent=6)

    def _link(self, parent: tk.Misc, w: cs.Window, url: str, name: str, indent: int, added: bool = False) -> None:
        edge = tk.Frame(parent, bg=LINK_EDGE, padx=1, pady=1)  # 枠 (Windows の tk.Button は枠線を描かないので包む)
        edge.pack(anchor=tk.W, padx=(indent, 4), pady=(2, 0))
        short = re.sub(r"^https?://", "", url)
        if len(short) > 38:  # 同じサイトの別のページが同じ見た目にならないよう、真ん中を省いて末尾を残す
            short = short[:16] + "…" + short[-20:]
        # 頼んで足したものは名前を前に出す (どれが本番か、を URL で読み分けなくて済むように)
        text = f"↗ {name}  {short}" if added and name else f"↗ {short}"
        if len(text) > 44:
            text = f"↗ {name}"
        b = tk.Button(edge, text=text, bg=CARD_BG, fg=LINK_FG, activebackground=LINK_HOVER,
                      activeforeground=LINK_FG, relief=tk.FLAT, bd=0, padx=6, pady=0, font=self.font_small,
                      cursor="hand2", command=lambda: webbrowser.open(url))
        b.pack()
        hover(b, CARD_BG, LINK_HOVER)
        Tooltip(b, "\n".join(x for x in (name, url, "右クリック: 隠す") if x))
        b.bind("<Button-3>", lambda e: self._link_menu(e, w, url, added))

    def _link_menu(self, e: tk.Event, w: cs.Window, url: str, added: bool) -> None:
        m = tk.Menu(self.frame, tearoff=False)
        m.add_command(label="開く", command=lambda: webbrowser.open(url))
        m.add_command(label="コピー", command=lambda: (self.frame.clipboard_clear(), self.frame.clipboard_append(url)))
        m.add_separator()
        if added:  # Claude に頼んで足したもの
            m.add_command(label="外す", command=lambda: self._edit_link("remove", url, w.folder))
        m.add_command(label=f"隠す: {w.name}", command=lambda: self._edit_link("hide", url, w.folder))
        m.add_command(label="隠す: 全部の窓", command=lambda: self._edit_link("hide", url, cs.ALL))
        m.tk_popup(e.x_root, e.y_root)

    def _edit_link(self, op: str, url: str, folder: str) -> None:
        try:
            cs.edit_links(op, url, folder, path=self.scanner.links_json)
        except (OSError, ValueError) as ex:
            self.status.configure(text=f"書けなかった: {ex}")
            return
        self._wake.set()  # 3 秒待たずに取り直す
