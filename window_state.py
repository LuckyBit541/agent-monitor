#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""cursor-float 的窗口状态与展示逻辑。

这里刻意不导入 PySide6：这些是纯计算，抽出来之后单元测试用任何 Python 都能跑，
不需要装 Qt。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Iterable, Sequence

from sessions import session_to_dict

STATE_PATH = Path.home() / ".cursor-float" / "window.json"

MODES = ("detail", "compact", "mini")
OPACITIES = (1.0, 0.85, 0.7, 0.55, 0.4, 0.25)
# 每种模式的宽度；高度由页面自测后上报
WIDTHS = {"detail": 400, "compact": 400, "mini": 246}
MIN_HEIGHT = 44
MAX_HEIGHT = 1400
DEFAULT_WIDTH = WIDTHS["detail"]

LOG = logging.getLogger("cursor-float")


def normalize_mode(value: Any) -> str:
    return value if value in MODES else "detail"


def normalize_opacity(value: Any) -> float:
    try:
        current = float(value)
    except (TypeError, ValueError):
        return 1.0
    if current in OPACITIES:
        return current
    return min(OPACITIES, key=lambda item: abs(item - current))


def normalize_collapsed(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return sorted({str(item) for item in value if str(item)})


def load_state(path: Path = STATE_PATH) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    payload["mode"] = normalize_mode(payload.get("mode"))
    payload["opacity"] = normalize_opacity(payload.get("opacity"))
    payload["collapsed"] = normalize_collapsed(payload.get("collapsed"))
    return payload


def save_state(payload: dict, path: Path = STATE_PATH) -> bool:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return True
    except OSError as exc:
        LOG.warning("无法保存窗口状态: %s", exc)
        return False


def saved_corner(state: dict) -> tuple[int, int]:
    try:
        return int(state.get("x", 0)), int(state.get("y", 0))
    except (TypeError, ValueError):
        return 0, 0


def initial_position(state: dict, area: tuple[int, int, int, int],
                     width: int = DEFAULT_WIDTH) -> tuple[int, int]:
    """把保存的位置拉回可视区域内（area = left, top, right, bottom）。

    实测出现过窗口被拖到屏幕底部之外、看起来像「消失」的情况（底边 1120 > 屏高
    1080），所以这里按窗口实际尺寸整体夹取，而不是只保证左上角非负。
    """
    left, top, right, bottom = area
    x, y = saved_corner(state)
    if x == 0 and y == 0:                       # 没保存过 -> 默认右上角
        x, y = right - width - 28, top + 48
    x = max(left, min(x, right - width))
    y = max(top, min(y, bottom - MIN_HEIGHT * 2))
    return x, y


def group_sessions(sessions: Sequence[Any], collapsed: Iterable[str]) -> list[dict]:
    """按项目分组，组间与组内都按最近活动时间倒序。"""
    folded = set(collapsed)
    buckets: dict[str, list] = {}
    for session in sessions:
        buckets.setdefault(session.project, []).append(session)
    for value in buckets.values():
        value.sort(key=lambda s: -s.last_active_ms)
    ordered = sorted(buckets.items(), key=lambda kv: -max(s.last_active_ms for s in kv[1]))
    return [
        {
            "project": project,
            "collapsed": project in folded,
            "warn": any(s.state == "waiting" for s in items),
            "sessions": [session_to_dict(s) for s in items],
        }
        for project, items in ordered
    ]


def signature(sessions: Sequence[Any], collapsed: Iterable[str], error: str) -> tuple:
    """用于判断「要不要推给页面」的指纹。

    只在真的变化时推送很重要：每次推送都会触发页面重排和高度自测，
    无脑推送会让窗口不停地重新测量。
    """
    return (
        tuple((s.composer_id, s.state, s.action_label, s.action_detail, s.title,
               s.project, int(s.elapsed_sec) // 10) for s in sessions),
        tuple(sorted(set(collapsed))),
        error,
    )


def payload(sessions: Sequence[Any], collapsed: Iterable[str], settings: dict,
            interval_sec: float, stamp: str, error: str = "") -> dict:
    """整理成卡片页面需要的完整结构。"""
    return {
        "settings": settings,
        "running": len(sessions),
        "waiting": sum(1 for s in sessions if s.state == "waiting"),
        "groups": group_sessions(sessions, collapsed),
        "stamp": stamp,
        "intervalSec": f"{max(0.2, float(interval_sec)):g}",
        "error": error,
    }
