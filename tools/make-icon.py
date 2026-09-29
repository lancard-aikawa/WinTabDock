# /// script
# requires-python = ">=3.11"
# dependencies = ["resvg-py", "pillow"]
# ///
"""assets/icon.svg と icon-small.svg から assets/icon.ico (と確認用の PNG) を作る (LocalLauncher と同じ作り方)。

    uv run tools/make-icon.py

32px 以下は icon-small.svg (簡略版) を使う。大きい絵を縮めると線が潰れるため。
"""

import io
from pathlib import Path

import resvg_py
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "assets"
SIZES = [16, 20, 24, 32, 40, 48, 64, 128, 256]


def render(svg: Path, size: int) -> Image.Image:
    png = resvg_py.svg_to_bytes(svg_path=str(svg), width=size, height=size)
    return Image.open(io.BytesIO(bytes(png))).convert("RGBA")


def main() -> None:
    images = [render(ASSETS / ("icon-small.svg" if s <= 32 else "icon.svg"), s) for s in SIZES]
    ico = ASSETS / "icon.ico"
    # Pillow は最大の画像から各サイズを作るので、サイズごとの絵は append_images で渡す
    images[-1].save(ico, format="ICO", sizes=[(s, s) for s in SIZES], append_images=images[:-1])
    images[-1].save(ASSETS / "icon-256.png")
    print(f"{ico.relative_to(ROOT)}: {ico.stat().st_size} bytes, {SIZES}")


if __name__ == "__main__":
    main()
