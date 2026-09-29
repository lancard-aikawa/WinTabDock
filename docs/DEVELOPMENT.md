# WinTabDock 開発メモ

使い方は [README](../README.md)。ここには、仕組みと、変えるときに守ること・試して分かったこと。

## ソースから動かす・exe を作る

```sh
uv run wintabdock.py              # wintabdock.toml のツールを並べたドックを開く (wintabdock.example.toml を写して作る)
uv run wintabdock.py --rescue     # ドックが落ちて隠れたままの窓を元に戻す
pwsh packaging/build.ps1        # dist/WinTabDock/ に WinTabDock.exe (窓なし) と _internal/ を作る
pwsh packaging/build.ps1 -Zip   # 配布用の dist/WinTabDock-<版>-win-x64.zip も作る (設定は見本を入れる)
uv run demo/shoot.py            # README の画面 (架空のデモアプリを取り込んだドック) を撮り直す
```

- 設定は exe の隣の `wintabdock.toml`。ビルド時に無ければ見本を置く (あれば上書きしない)
- 「設定」の「ログイン時に起動する」で、HKCU の Run に exe の場所を登録する。exe を移動したら入れ直す
- 窓なしの exe では `sys.stdout` が `None`。`sys.stdout.reconfigure()` をそのまま呼ぶと起動しなかった
- アイコンの元は `assets/icon.svg` (48px 以上) と `assets/icon-small.svg` (32px 以下)。`uv run tools/make-icon.py` で ICO を作り直す
- exe の確かめ方: `uv run check_exe.py`。exe を一時フォルダにコピーし、PortWho と RepoTether だけの設定と
  `WINTABDOCK_HOME` (控えと前回のタブの置き場所) を一時フォルダに向けて動かす。人の前回のタブや他のツールには触れない

## 仕組み (仮想タブ)

相手の窓の中に手を入れず、トップレベルの窓のまま扱う。

- 取り込むと、タイトルバーと縁を外し、**窓の所有者 (owner) をドックにして**本体の上に重ねる
  (`SetWindowLongPtr(GWLP_HWNDPARENT)`)。所有される窓は Windows が常に所有者より手前に置き、最小化も一緒になり、
  タスクバーのボタンも出ない。念のため `WS_EX_TOOLWINDOW` も付ける
- 所有者を付け替えた直後や隠していた窓を出した直後は、重なり順がまだ組み直されていない。ドックを一度 `HWND_TOP` に動かす
- 選んでいないタブの窓は隠す。ドックの移動・大きさ変更に合わせて、選んでいる窓を動かす (重なり順には触らない)
- 取り込んだ窓はタイトルバーを外して隠すので、窓を探しても見つからない。二重起動を避けるため、タブに exe を覚えておく
- 元に戻すときは、属性を戻して見せたあと `ITaskbarList::AddTab` でタスクバーのボタンを明示的に足す。属性が元どおりでも
  タスクバーがボタンを付け直さないことがあった (自動で取り込んだ PortWho が、ドックを閉じたあとタスクバーに出なかった)
- 取り込んだ窓の元の状態 (枠・所有者・位置) は `%LOCALAPPDATA%\WinTabDock\state-<プロセス番号>.json` に控える。
  ドックが落ちても相手の窓とプロセスは残り (所有者が外れるだけ)、次の起動か `--rescue` で元の枠に戻る。
  戻すのは、もう動いていないドックの控えだけ。1 つの `state.json` を共有していたときは、2 つ目のドックが
  動いている方のドックの控えを「前回落ちたもの」と取り違えて窓を戻し、控えを消した。
  以前の共有の `state.json` は、ほかにドックが開いていないときの `--rescue` でだけ扱う

### 採らなかったやり方 (試して問題が出たもの)

最初は所有者を変えず、前面の窓の変化を見張って重なり順を自分で直していた。次の問題が順に出て、所有者方式に切り替えた。

- 別プロセスの窓は `HWND_TOP` で上げても前面の窓より上に上がらず、ドックのタイトルバーで戻るとツールが他のアプリの後ろに残った
- `SetForegroundWindow` でツールを前面にすると、枠やタイトルバーを押した瞬間に前面が移ってリサイズ・移動が取り消された
- ボタンを離すまで待つと、押している間 (移動・リサイズ・「+ 起動」) は本体が真っ暗に見えた
- 前面の変化 (WinEvent) を tkinter の mainloop と同じスレッドで受けると、Python ごと落ちた
  (`PyEval_RestoreThread: the GIL is released`)。別スレッドで受ける必要があった
- 所有者を変えると、ドックが落ちたときに相手の窓まで壊れる、と心配していたが、実験では窓もプロセスも残った
  (`experiments/owner_probe.py`)

## 確かめ方 (自分のツール PortWho・RepoTether などを使う。対象のツールとほかのドックを閉じてから流す)

```sh
uv run wintabdock.py --selftest LPortMan PortWho   # 取り込み・二重起動・切り替え・リサイズ・移動 (途中も見えるか)・メニュー・最小化・別アプリとの重なり・起動時に開く・閉じるときのツール・外す・閉じる・自動取り込み (48 項目)
uv run check_rescue.py LPortMan                  # 取り込んだまま落として、--rescue で戻るか
uv run check_state_files.py                      # 動いているドックの控えに触らないか (窓には触れない)
uv run check_session.py PortWho RepoTether       # 前回のタブ: 起動中なら取り込み、なければ起動して並べ直すか
uv run check_startup.py PortWho RepoTether       # 起動時に開く: 選び方ごとに開くもの・起動するものが変わるか
uv run check_taskbar.py PortWho RepoTether       # ドックを閉じたあと、ツールがタスクバーに戻るか (UI Automation で見る)
uv run check_close.py PortWho RepoTether         # × を押したとき: 一緒に閉じる / 残す / たずねる / × を無視するアプリ
```

- 試験で起動する補助のアプリは、出力を引き継がせない (`stdout=DEVNULL`)。引き継ぐと、補助のアプリが試験の出力の
  パイプを持ち続け、試験を指揮するスクリプトが出力の終わりを待って止まった

- タスクバーに出るかは、窓の属性 (所有者・拡張スタイル) では分からない。属性は元どおりでもボタンが無いことがあった。
  タスクバーのボタン名を UI Automation で読んで確かめる

- 試験のドックが扱うのは、試験の対象に挙げたツールだけ (自動取り込みで、人が使っている他のツールを取り込まないように)。
  前回のタブも人のもの (`session.json`) とは別の `session-selftest.json` を使う
- 前回のタブの試験は、空のタブを復元しても「開き直したタブ = 覚えたタブ」で通ってしまった。期待する並びを試験の側で持つ

- **試験は、対象のツールとほかのドックを閉じてから流す。** 試験の「起動」は起動中の窓を取り込み、最後に閉じるので、
  人が使っているツールを閉じてしまう (RepoTether と PortWho を閉じてしまった)。今は始める前に確かめて止まる

- 位置と表示状態だけ見ると、窓がドックの後ろに回っていても通ってしまう。`WindowFromPoint` が
  その窓を返すか (手前に見えているか) まで見ること
- タブの切り替え・別のアプリから戻る操作・枠のドラッグは、本物のマウスで行う (試験中はカーソルが動く)。
  プログラムから前面化すると、Windows の扱いが人の操作と違い、実際には起きない失敗や、実際に起きる失敗の見逃しになる
