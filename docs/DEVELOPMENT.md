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

## 遠隔で使う (VNC の試作、2026-10-26)

目標: Android から WinTabDock につなぎ、スマホの画面の大きさにドックを合わせて使う。まだドックには入れていない。
`experiments/vnc_probe.py` が 1 つの窓を VNC で見せる (ドックに入れる前の確かめ)。

```sh
uv run experiments/vnc_probe.py --title PortWho --offscreen   # http://127.0.0.1:15900/ を開く (noVNC が出る)
uv run experiments/vnc_probe.py --exe C:/Tools/x.exe --offscreen --host <Tailscale のアドレス>
uv run experiments/vnc_probe_check.py                         # 試験用の窓 (vnc_testapp.py) で 29 項目
uv run experiments/vnc_android_check.py                       # Android の実機 (USB デバッグ) の Chrome から 8 項目
```

- ポート 15900 (LPortMan の台帳に登録)。同じポートで、ブラウザ (WebSocket) と VNC アプリ (TCP のまま) の両方を受ける。
  最初の 0.4 秒に `GET` が来ればブラウザ、何も来なければ VNC アプリ (VNC はサーバが先に挨拶する)
- ブラウザの側は noVNC 1.6.0。初回に GitHub から取ってハッシュを確かめ、`%LOCALAPPDATA%\WinTabDock\novnc-1.6.0\` に置く。
  `resize=remote` で、ブラウザの大きさ (CSS の px) を SetDesktopSize で送ってくる。スマホなら 412x900 前後になるので、
  文字が豆粒になる心配は小さい。noVNC は、サーバから ExtendedDesktopSize を一度受けるまで大きさを送ってこない
- 画像は Tight の JPEG で送る (変わった範囲だけ)。1 枚撮って送るのに 60〜100ms
- 認証は無い。インターネットに開けない (Tailscale の中だけで使う)
- WebSocket は、`Origin` が `http://<Host>` で、`Host` が待ち受けのアドレスか 127.0.0.1・localhost のときだけ受ける。
  確かめないと、ブラウザで開いているだけのよそのサイトのページが 127.0.0.1:15900 につないで窓を操作できる
  (DNS を書き換えて別の名前で来る手口も Host で断る)。VNC アプリの TCP の接続はブラウザからは作れない

### Android の実機での試験 (2026-10-27、moto g05 / Android 15 / Chrome 155)

- `adb reverse tcp:15900` でスマホの 127.0.0.1:15900 を PC に届ける (Tailscale もポートを開けることも要らない)。
  スマホの Chrome の開発用の口を `adb forward tcp:29222 localabstract:chrome_devtools_remote` で PC に出し、
  本物のタッチ (`Input.dispatchTouchEvent`) と、日本語の確定文字 (`Input.insertText`) を noVNC の入力欄に送る
- 試験は画面の向きを横にして戻す (回転の設定を控えて最後に戻す)。開いたタブも閉じる。ポート 29222 は LPortMan の台帳に登録
- 縦向きはブラウザの中が 411x772 (devicePixelRatio 1.75)、横向きは 828x315。どちらも窓がその大きさになり、
  タップ・長押し・「abc日本語」の入力が通った
- **noVNC が同じ大きさを 1 秒に 10 回頼み続けた。** 幅が 411.43 のように端数になる画面で、受け付けたと返すたびに noVNC が
  表示を作り直し、その拍子に大きさの監視が反応してまた頼んでくる。今と同じ大きさの頼みには「変えない」(状態 1) と返して止めた。
  この間は最初のタップが時々抜けていたが、止めてからは 3 回続けて全部通った
- 画面のキーボードを出すと、その分だけブラウザの中が狭くなる (画面写真では下の端がキーボードに隠れた)。大きさを頼み直すかは未確認

### 試して分かったこと

- **撮る**: `PrintWindow(PW_CLIENTONLY | PW_RENDERFULLCONTENT)` で、隠した窓・画面の外の窓・ほかの窓の後ろの窓も撮れた (Tk)
- **大きさ**: 枠のある窓は、Windows が画面の高さより大きくしない (1080 の画面で内側 1061 まで)。
  PortWho は幅 680、LPortMan は 760 より狭くならない。届かなかったときは、画像を縮めて要求された大きさに収め、
  縮めた分だけ窓を縦に伸ばす。クリックの座標も同じ倍率で戻す。PortWho を幅 412 に収めると文字は約 0.6 倍
- **押す (Tk)**: 窓へのメッセージ (`WM_LBUTTONDOWN`) だけでは押されない。Tk は押したかをマウスの状態 (`GetKeyState`) で見る。
  `AttachThreadInput` で相手と入力の状態を共有し、`SetKeyboardState` でボタンを押した状態にしてから
  `SendMessageTimeout` で送る (相手が処理し終わるまで待つ。投げっぱなしだと、状態を戻すのが先になりうる)
- **押す (Tk)**: 押したまま 0.3 秒待つと押されなかった (スマホのタップは 0.1 秒ほど押している)。Tk は本物のマウスカーソルの
  位置を定期的に見に行き、そこに自分が無いと「出た」ことにする。押している間は 20ms ごとに同じ位置への移動を送り続ける。
  試験が時々落ちた (25 項目中 4 つ) のもこれだった
- **押す (Tk)**: 押した所の画面に**別の窓が重なっていると押されない** (最前面の窓で再現)。画面の外に置けば重なりが無く、
  押せる。`--offscreen` はつないでいる間、窓を画面の右の外に置き、終わるときに戻す
- **打つ (Tk)**: Tk は、自分の窓が前面でないと文字を捨てる。押すたびに `WM_ACTIVATE` と `WM_SETFOCUS` を送ると受け付ける
  (本物の前面・フォーカスは動かさない)。文字は `WM_CHAR` で送る。noVNC はスマホの日本語の確定文字を Unicode の keysym
  (`0x01000000 | 文字コード`) で送ってくるので、そのまま `WM_CHAR` にできる (日本語の「あ」で確かめた。スマホの実機は未確認)
- **修飾キー** (Ctrl・Shift・Alt) は窓へのメッセージでは効かない (相手は本物のキーボードの状態を読む)。未対応
- **Chromium (Edge のアプリ窓で確かめた。WebView2 の RepoTether は未確認)**: 画面の上なら、押す・打つ・大きさの変更が効いた。
  画面の外では描くのを止め、灰色が写り、ページの大きさも変わらなかった。起動時に
  `--disable-features=CalculateNativeWinOcclusion --disable-backgrounding-occluded-windows` を付けると、画面の外でも効いた。
  WebView2 なら環境変数 `WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS` で渡せるはず (未確認)
- `--input sendinput` (本物の入力) は書いただけで試していない。手元のマウスを動かし、画面の上の窓にしか届かない
- 画面ロック中に撮れるか・押せるかは試していない (無人の PC をロックすると、試験の後で戻せない)

### 試験で踏んだこと

- 試験用の窓をタイトルの頭 (`vnc-testapp`) だけで探したら、noVNC を開いたブラウザの窓 (題がタイトルを写す) をつかみ、
  ブラウザに文字を打ち込んだ。窓の種類 (`TkTopLevel`) と、起動ごとの目印をタイトルに入れて探す
- noVNC のタブを開いたまま試験を流すと、自動でつなぎ直してブラウザの大きさを要求し、試験の窓の大きさを変えた。先に閉じる
- uv の .venv の `python.exe` は起動役で、窓を持つのは別のプロセス。PortWho (PyInstaller の 1 ファイルの exe) も子プロセスが窓を持つ。
  起動したプロセスの番号では窓を探せない。exe のパスかタイトルで探す
- LPortMan (`lportmanw.exe`) は、stdout に DEVNULL を渡して起動すると、窓を出さずにすぐ終わった。`--exe` では出力をつながない
- 出し直した (`ShowWindow`) 直後の Tk の窓は、子窓がまだ隠れていて、押した先が外れる。少し待つ

### 次にすること

1. Android の実機を人の手で触る (Tailscale 越し)。タッチの操作感、画面のキーボードで本当に打つ日本語、文字の小ささ
2. ドックに入れる: 撮るのはドックの内側 (タブの帯 + 選んでいるツール)。押した所がタブの帯ならドック、それ以外はツールに送る。
   つないでいる間はドックごと画面の外に置く。落ちたときに画面の外に残らないよう、`--rescue` の控えに位置を入れる
3. 文字の小ささ: noVNC は CSS の px で大きさを送るので、スマホの細かい画素を使えていない。縮めて収めるときに特に効く
