#!/usr/bin/env python3
"""直したい所を白、残す所を黒にしたマスク画像（PNG）を作る。標準ライブラリだけで動く。

位置は画像の幅・高さに対する割合（0〜1）で指定する。
  python tools/make_mask.py --size 1216x832 --rect 0.30,0.35,0.62,0.95 --out works/mug/mask.png
  python tools/make_mask.py --like works/mug/outputs/a.png --ellipse 0.3,0.35,0.62,0.95 --grow 12 --soft 16 --out mask.png
"""
from __future__ import annotations

import argparse
import struct
import sys
import zlib
from pathlib import Path

for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8")
    except Exception:
        pass


def png_size(path: Path) -> tuple[int, int]:
    head = path.read_bytes()[:24]
    if head[:8] != b"\x89PNG\r\n\x1a\n":
        sys.exit("--like には PNG を指定してください")
    return struct.unpack(">II", head[16:24])


def write_png(path: Path, w: int, h: int, rows: list[bytearray]) -> None:
    raw = b"".join(b"\x00" + bytes(r) for r in rows)

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 0, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b"")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(png)


def parse_box(text: str) -> tuple[float, float, float, float]:
    x0, y0, x1, y1 = (float(v) for v in text.split(","))
    if not (0 <= x0 < x1 <= 1 and 0 <= y0 < y1 <= 1):
        sys.exit(f"位置は 0〜1 の割合で 左,上,右,下 の順に: {text}")
    return x0, y0, x1, y1


def main() -> None:
    ap = argparse.ArgumentParser(description="直したい所を白にしたマスクPNGを作る")
    ap.add_argument("--size", help="幅x高さ（例 1216x832）")
    ap.add_argument("--like", help="この PNG と同じ大きさにする")
    ap.add_argument("--rect", action="append", default=[], help="四角 左,上,右,下（0〜1の割合）")
    ap.add_argument("--ellipse", action="append", default=[], help="だ円 左,上,右,下（0〜1の割合）")
    ap.add_argument("--grow", type=int, default=0, help="白い所を外側へ広げる画素数")
    ap.add_argument("--soft", type=int, default=0, help="縁をぼかす幅（画素）。境目のつなぎ目を目立ちにくくする")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    if a.like:
        w, h = png_size(Path(a.like))
    elif a.size:
        w, h = (int(v) for v in a.size.lower().split("x"))
    else:
        sys.exit("--size か --like を指定してください")
    rows = [bytearray(w) for _ in range(h)]
    g, soft = a.grow, max(0, a.soft)

    def put(x: int, y: int, inside: float) -> None:
        # inside: 図形の縁から内側への距離（画素）。soft の幅で 0→255 に上げる
        v = 255 if soft == 0 or inside >= soft else int(255 * max(0.0, inside) / soft)
        if v > rows[y][x]:
            rows[y][x] = v

    for spec in a.rect:
        x0, y0, x1, y1 = parse_box(spec)
        xa, xb, ya, yb = x0 * w - g, x1 * w + g, y0 * h - g, y1 * h + g
        for y in range(max(0, int(ya)), min(h, int(yb) + 1)):
            for x in range(max(0, int(xa)), min(w, int(xb) + 1)):
                d = min(x + 0.5 - xa, xb - x - 0.5, y + 0.5 - ya, yb - y - 0.5)
                if d > 0:
                    put(x, y, d)
    for spec in a.ellipse:
        x0, y0, x1, y1 = parse_box(spec)
        cx, cy = (x0 + x1) / 2 * w, (y0 + y1) / 2 * h
        rx, ry = (x1 - x0) / 2 * w + g, (y1 - y0) / 2 * h + g
        for y in range(max(0, int(cy - ry)), min(h, int(cy + ry) + 1)):
            for x in range(max(0, int(cx - rx)), min(w, int(cx + rx) + 1)):
                r = (((x + 0.5 - cx) / rx) ** 2 + ((y + 0.5 - cy) / ry) ** 2) ** 0.5
                if r < 1:
                    put(x, y, (1 - r) * min(rx, ry))
    write_png(Path(a.out), w, h, rows)
    print(f"マスクを作りました: {a.out}（{w}x{h}、白が直す所）")


if __name__ == "__main__":
    main()
