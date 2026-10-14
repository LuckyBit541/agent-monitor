#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""cursor-float 的窗口状态与展示逻辑。

这里刻意不导入 PySide6：这些是纯计算，抽出来之后单元测试用任何 Python 都能跑，
不需要装 Qt。
"""

from __future__ import annotations

import json
import logging
import time as _time
from pathlib import Path
from typing import Any, Iterable, Sequence

from sessions import Session, session_to_dict

STATE_PATH = Path.home() / ".cursor-float" / "window.json"

MODES = ("detail", "compact", "mini")
OPACITIES = (1.0, 0.85, 0.7, 0.55, 0.4, 0.25)
# 每种模式的宽度；高度由页面自测后上报
WIDTHS = {"detail": 400, "compact": 400, "mini": 272}
MIN_HEIGHT = 44
MAX_HEIGHT = 1400
DEFAULT_WIDTH = WIDTHS["detail"]

# 贴边收起后的细条尺寸
HANDLE_W = 22
HANDLE_H = 76

# 窗口边缘离屏幕边缘多近才算「在边上」
SNAP_DIST = 24

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
    payload["dock"] = bool(payload.get("dock", True))
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


def dock_side(area: tuple[int, int, int, int], x: int, width: int) -> str:
    """贴在左边缘还是右边缘——取更近的那一边。"""
    left, _top, right, _bottom = area
    return "right" if (x + width / 2) >= (left + right) / 2 else "left"


def edge_at(area: tuple[int, int, int, int], x: int, width: int,
            snap: int = SNAP_DIST) -> str | None:
    """窗口离哪条竖边足够近就返回它，否则返回 None。

    贴边收起是**位置驱动**的：只有窗口真的在边上才收起，在屏幕中间就是普通浮窗。
    """
    left, _top, right, _bottom = area
    if x - left <= snap:
        return "left"
    if right - (x + width) <= snap:
        return "right"
    return None


def folded_geometry(area: tuple[int, int, int, int], x: int, y: int,
                    width: int, height: int) -> tuple[int, int]:
    """收起成细条后的左上角：贴到最近的那条竖边，纵向以原来中心为准并夹在屏幕内。"""
    left, top, _right, bottom = area
    hx = left if dock_side(area, x, width) == "left" else area[2] - HANDLE_W
    cy = y + height // 2
    hy = max(top, min(cy - HANDLE_H // 2, bottom - HANDLE_H))
    return hx, hy


def expanded_geometry(area: tuple[int, int, int, int], x: int, y: int,
                      width: int, height: int) -> tuple[int, int]:
    """从细条展开时的左上角：横向贴回那条边，纵向以细条中心为准并夹在屏幕内。"""
    left, top, _right, bottom = area
    ex = left if dock_side(area, x, HANDLE_W) == "left" else area[2] - width
    ey = max(top, min(y + HANDLE_H // 2 - height // 2, bottom - height - 4))
    return ex, ey


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


def demo_sessions(now_ms: int | None = None) -> list[Session]:
    """--demo 用的编造会话。

    刻意做成「一个项目下两个会话 + 另外两个项目各一个」，用来检查分组与折叠。
    **这里必须是编造的名字**：它会出现在 README 的截图里，填真实项目名就等于
    把内部项目名发到公开仓库上。
    """
    t = now_ms if now_ms is not None else int(_time.time() * 1000)
    return [
        Session(
            composer_id="demo-1", project="acme-web · feat-auth-refresh",
            title="修复登录态刷新竞态", state="running",
            action_label="执行命令",
            action_detail='npm run test:e2e -- --grep "token refresh"',
            elapsed_sec=83, last_active_ms=t - 1000, signals=["tool=running"],
        ),
        Session(
            composer_id="demo-2", project="acme-web · feat-auth-refresh",
            title="补 auth 模块单测", state="running",
            action_label="生成回复中",
            action_detail="正在写 tests/auth/refresh.spec.ts", elapsed_sec=21,
            is_subagent=True, subagent_type="generalPurpose",
            last_active_ms=t - 3000, signals=["generatingBubbleIds"],
        ),
        Session(
            composer_id="demo-3", project="shop-api", title="给下单接口加幂等键",
            state="running", action_label="编辑文件",
            action_detail="src/order/idempotency.ts", elapsed_sec=132,
            last_active_ms=t - 20000, signals=["tool=running"],
        ),
        Session(
            composer_id="demo-4", project="data-pipeline", title="排查 worker 内存溢出",
            state="waiting", action_label="执行命令",
            action_detail="docker compose restart worker",
            elapsed_sec=310, last_active_ms=t - 60000, signals=["hasBlockingPendingActions"],
        ),
    ]


def payload(sessions: Sequence[Any], collapsed: Iterable[str], settings: dict,
            interval_sec: float, stamp: str, error: str = "") -> dict:
    """整理成卡片页面需要的完整结构。"""
    return {
        "settings": settings,
        # 各形态的宽度由这里说了算，页面不要自己再存一份（曾经漂移过）
        "widths": dict(WIDTHS),
        "running": len(sessions),
        "waiting": sum(1 for s in sessions if s.state == "waiting"),
        "groups": group_sessions(sessions, collapsed),
        "stamp": stamp,
        "intervalSec": f"{max(0.2, float(interval_sec)):g}",
        "error": error,
    }
