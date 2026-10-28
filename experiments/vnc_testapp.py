"""vnc_probe の試験に使う窓 (vnc_probe_check.py が起動する)。

    uv run experiments/vnc_testapp.py [目印 [最小の幅]]

ボタンを押した回数・入力欄の文字・窓の大きさをタイトルに出すので、外から GetWindowText で確かめられる。
四隅に色の付いた札を置くので、撮った画像が窓全体か (端が切れていないか) も分かる。
"""

import ctypes
import os
import sys
import time
import tkinter as tk

ctypes.windll.shcore.SetProcessDpiAwareness(2)

root = tk.Tk()
root.geometry("640x480+120+120")
root.configure(bg="#ffffff")
count = 0
text = tk.StringVar()
events: list[str] = []  # 受けた出来事 (直近 6 つ)。操作が届かないときに、どこで止まったかを見る


TAG = sys.argv[1] if len(sys.argv) > 1 else "-"  # 試験が窓を見分ける目印 (noVNC の題もタイトルを写すので、頭だけでは足りない)
if len(sys.argv) > 2:  # 最小の幅 (PortWho・LPortMan のように、それより狭くならない窓の代わり)
    root.minsize(int(sys.argv[2]), 1)


def update_title(*_):
    root.title(f"vnc-testapp {TAG} count={count} text={text.get()} size={root.winfo_width()}x{root.winfo_height()}"
               f" ev={','.join(events[-6:])}")


LOG = os.environ.get("VNC_TESTAPP_LOG")  # 受けた出来事を全部書き出す (試験が時々落ちるのを調べる)


def note(ev):
    events.append(f"{ev.type}@{ev.widget.winfo_class()}")
    if LOG:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(f"{time.monotonic():.3f} {TAG} {ev.type} {ev.widget.winfo_class()} {getattr(ev, 'x', '')} "
                    f"{getattr(ev, 'y', '')} {getattr(ev, 'keysym', '')}\n")
    update_title()


def press():
    global count
    count += 1
    update_title()


for color, anchor in (("#e53935", "nw"), ("#43a047", "ne"), ("#1e88e5", "sw"), ("#fdd835", "se")):
    tk.Label(root, bg=color, width=4, height=2).place(relx=1.0 if "e" in anchor else 0.0,
                                                      rely=1.0 if "s" in anchor else 0.0, anchor=anchor)
tk.Button(root, text="押す", font=("", 16), command=press).place(x=60, y=80, width=160, height=60)
entry = tk.Entry(root, textvariable=text, font=("", 16))
entry.place(x=60, y=170, width=300, height=40)
text.trace_add("write", update_title)
root.bind("<Configure>", update_title)
entry.bind("<Control-s>", lambda e: (text.set(text.get() + "[S]"), entry.icursor("end"), "break")[-1])  # Ctrl を遠隔から
for seq in ("<ButtonPress>", "<ButtonRelease>", "<Enter>", "<Leave>", "<KeyPress>", "<FocusIn>") + (("<FocusOut>",) if LOG else ()):
    root.bind_all(seq, note, add="+")
update_title()
if os.environ.get("VNC_TESTAPP_SECOND"):  # 同じプロセスのもう 1 つの普通の窓 (VS Code の 2 つ目の窓の代わり)
    second = tk.Toplevel(root)
    second.title(f"vnc-testapp {TAG}-second")
    second.geometry("300x200+700+160")
root.mainloop()
