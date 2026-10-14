#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""开发辅助：依次切到三种显示模式与一档低透明度截图，并合成 README 的预览图。

    python tools/capture.py

单张截图放在 _shots/，合成结果写到根目录的 _preview.png。这个脚本不参与浮窗运行。
需要 Pillow。
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SHOTS = ROOT / "_shots"
PREVIEW = ROOT / "_preview.png"

# (文件名, 模式, 透明度, 是否贴边收起)
PLAN = [
    ("detail", "detail", 1.0, False),
    ("compact", "compact", 1.0, False),
    ("mini", "mini", 1.0, False),
    ("alpha55", "detail", 0.55, False),
    ("folded", "detail", 1.0, True),
]


def run_shot(name: str, mode: str, opacity: float, fold: bool = False) -> Path:
    """用 --shot 起一次浮窗，让它自己截完图退出。"""
    state_dir = Path.home() / ".cursor-float"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "window.json").write_text(
        f'{{"mode": "{mode}", "opacity": {opacity}, "pinned": true, '
        f'"dock": {str(fold).lower()}, "collapsed": []}}',
        encoding="utf-8",
    )
    target = SHOTS / f"{name}.png"
    target.parent.mkdir(parents=True, exist_ok=True)
    argv = [sys.executable, str(ROOT / "app.py"), "--mode", mode,
            "--demo", "--shot", str(target)]
    if fold:
        argv.append("--fold")
    subprocess.run(argv, cwd=str(ROOT), check=False)
    return target


def compose() -> None:
    """把四张截图竖着拼成一张预览图，各自加一点间距。"""
    from PIL import Image

    images = []
    for name, _mode, _opacity, fold in PLAN:
        path = SHOTS / f"{name}.png"
        if not path.exists():
            continue
        img = Image.open(path).convert("RGBA")
        if fold:
            # 细条太窄，拼图时放大三倍，否则在预览里看不清
            img = img.resize((img.width * 3, img.height * 3), Image.NEAREST)
        images.append(img)
    if not images:
        print("没有可用截图")
        return

    gap, pad = 14, 18
    width = max(img.width for img in images) + pad * 2
    height = sum(img.height for img in images) + gap * (len(images) - 1) + pad * 2
    canvas = Image.new("RGBA", (width, height), (13, 16, 21, 255))
    y = pad
    for img in images:
        # 贴边的细条靠右摆，才看得出它是贴着屏幕边的
        x = width - pad - img.width if img.width * 3 < width else pad
        canvas.alpha_composite(img, (x, y))
        y += img.height + gap
    canvas.convert("RGB").save(PREVIEW)
    print(f"已写出 {PREVIEW}  ({canvas.width}x{canvas.height}, {len(images)} 种形态)")


def main() -> int:
    for name, mode, opacity, fold in PLAN:
        path = run_shot(name, mode, opacity, fold)
        print(f"{name:8} mode={mode:8} opacity={opacity:<5} "
              f"{'收起' if fold else '展开'} -> {path.name} "
              f"{'(缺少)' if not path.exists() else ''}")
        time.sleep(0.5)
    compose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
