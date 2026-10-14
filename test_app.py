#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""窗口状态与展示逻辑的单元测试。

这一组不依赖 PySide6，用任何 Python 都能跑。
"""

from __future__ import annotations

import shutil
import unittest
import uuid
from pathlib import Path

import window_state as ws
from sessions import Session

TMP_ROOT = Path(__file__).resolve().parent / ".tmp-test"


def make_session(cid="c1", project="proj", title="任务", action="执行命令",
                 detail="npm run build", elapsed=12.0, state="running",
                 last_active=1000, subagent=False):
    return Session(
        composer_id=cid, project=project, title=title, state=state,
        action_label=action, action_detail=detail, elapsed_sec=elapsed,
        is_subagent=subagent, last_active_ms=last_active, signals=[],
    )


class NormalizeTest(unittest.TestCase):
    def test_mode(self):
        for good in ws.MODES:
            self.assertEqual(ws.normalize_mode(good), good)
        for bad in (None, "", "detailx", 3, ["mini"]):
            self.assertEqual(ws.normalize_mode(bad), "detail")

    def test_opacity_snaps_to_nearest_allowed(self):
        self.assertEqual(ws.normalize_opacity(0.7), 0.7)
        self.assertEqual(ws.normalize_opacity(0.84), 0.85)
        self.assertEqual(ws.normalize_opacity(0.6), 0.55)
        self.assertEqual(ws.normalize_opacity(0), 0.25)
        self.assertEqual(ws.normalize_opacity(9), 1.0)

    def test_opacity_bad_input_is_full(self):
        for bad in (None, "abc", object()):
            self.assertEqual(ws.normalize_opacity(bad), 1.0)

    def test_collapsed(self):
        self.assertEqual(ws.normalize_collapsed(["b", "a", "a"]), ["a", "b"])
        self.assertEqual(ws.normalize_collapsed("a"), [])
        self.assertEqual(ws.normalize_collapsed(None), [])
        self.assertEqual(ws.normalize_collapsed([1, ""]), ["1"])


class StateFileTest(unittest.TestCase):
    def setUp(self):
        # 不用 tempfile.mkdtemp：它建的目录带 owner-only 权限，
        # 在受限沙箱里后续访问会被拒绝。
        TMP_ROOT.mkdir(exist_ok=True)
        self.dir = TMP_ROOT / f"ws-{uuid.uuid4().hex[:8]}"
        self.dir.mkdir()
        self.path = self.dir / "window.json"
        self.addCleanup(lambda: shutil.rmtree(self.dir, ignore_errors=True))

    def test_missing_file_gives_defaults(self):
        state = ws.load_state(self.path)
        self.assertEqual(state["mode"], "detail")
        self.assertEqual(state["opacity"], 1.0)
        self.assertEqual(state["collapsed"], [])
        # 贴边收起默认开着：但它是位置驱动的，窗口在屏幕中间时行为不变
        self.assertTrue(state["dock"])

    def test_round_trip(self):
        original = {"mode": "mini", "opacity": 0.4, "pinned": False,
                    "collapsed": ["a"], "dock": True, "x": 10, "y": 20}
        self.assertTrue(ws.save_state(original, self.path))
        self.assertEqual(ws.load_state(self.path), original)

    def test_dock_can_be_turned_off_and_stays_off(self):
        self.path.write_text('{"mode": "detail", "dock": false}', encoding="utf-8")
        self.assertFalse(ws.load_state(self.path)["dock"])

    def test_corrupt_file_gives_defaults(self):
        self.path.write_text("{ not json", encoding="utf-8")
        state = ws.load_state(self.path)
        self.assertEqual(state["mode"], "detail")

    def test_non_dict_json_gives_defaults(self):
        self.path.write_text("[1, 2, 3]", encoding="utf-8")
        self.assertEqual(ws.load_state(self.path)["mode"], "detail")

    def test_values_are_normalized_on_load(self):
        self.path.write_text('{"mode": "nope", "opacity": 0.84, "collapsed": "x"}',
                             encoding="utf-8")
        state = ws.load_state(self.path)
        self.assertEqual(state["mode"], "detail")
        self.assertEqual(state["opacity"], 0.85)
        self.assertEqual(state["collapsed"], [])


class PositionTest(unittest.TestCase):
    AREA = (0, 0, 1920, 1080)

    def test_default_is_top_right(self):
        x, y = ws.initial_position({}, self.AREA, 400)
        self.assertEqual(x, 1920 - 400 - 28)
        self.assertEqual(y, 48)

    def test_saved_position_is_kept_when_visible(self):
        x, y = ws.initial_position({"x": 300, "y": 200}, self.AREA, 400)
        self.assertEqual((x, y), (300, 200))

    def test_offscreen_position_is_pulled_back(self):
        """拖到屏幕右侧之外要能拉回来（实测出现过窗口跑到屏幕外）。"""
        x, y = ws.initial_position({"x": 5000, "y": 40}, self.AREA, 400)
        self.assertEqual(x, 1920 - 400)

    def test_negative_position_is_pulled_back(self):
        x, y = ws.initial_position({"x": -800, "y": -50}, self.AREA, 400)
        self.assertEqual((x, y), (0, 0))

    def test_tall_saved_y_stays_on_screen(self):
        """实测过的场景：位置 (1279, 960) 在 1080 高的屏幕上会让底边超出。"""
        x, y = ws.initial_position({"x": 1279, "y": 960}, self.AREA, 400)
        self.assertLess(y, 1080)
        self.assertGreaterEqual(y, 0)

    def test_bad_coordinates_fall_back(self):
        x, y = ws.initial_position({"x": "abc", "y": None}, self.AREA, 400)
        self.assertEqual(x, 1920 - 400 - 28)


class DockingTest(unittest.TestCase):
    AREA = (0, 0, 1920, 1080)

    def test_side_is_the_nearer_edge(self):
        self.assertEqual(ws.dock_side(self.AREA, 1492, 400), "right")
        self.assertEqual(ws.dock_side(self.AREA, 20, 400), "left")
        # 窗口中心正好在中线（960）左侧 -> 贴左；跨过中线 -> 贴右
        self.assertEqual(ws.dock_side(self.AREA, 700, 400), "left")
        self.assertEqual(ws.dock_side(self.AREA, 800, 400), "right")

    def test_edge_at_only_reports_near_edges(self):
        """贴边收起必须是位置驱动的：只有真的在边上才算。"""
        self.assertEqual(ws.edge_at(self.AREA, 1920 - 400, 400), "right")   # 已贴右
        self.assertEqual(ws.edge_at(self.AREA, 1880, 400), "right")         # 差 40 之内
        self.assertEqual(ws.edge_at(self.AREA, 0, 400), "left")             # 已贴左
        self.assertEqual(ws.edge_at(self.AREA, 20, 400), "left")            # 差 20
        # 屏幕中间：不算贴边，行为应保持普通浮窗
        self.assertIsNone(ws.edge_at(self.AREA, 500, 400))
        self.assertIsNone(ws.edge_at(self.AREA, 960, 400))
        self.assertIsNone(ws.edge_at(self.AREA, 1400, 400))

    def test_folded_sticks_to_the_edge_and_keeps_vertical_center(self):
        x, y = ws.folded_geometry(self.AREA, 1492, 300, 400, 144)
        self.assertEqual(x, 1920 - ws.HANDLE_W)
        self.assertEqual(y, 300 + 144 // 2 - ws.HANDLE_H // 2)

    def test_folded_on_left_edge(self):
        x, _y = ws.folded_geometry(self.AREA, 20, 300, 400, 144)
        self.assertEqual(x, 0)

    def test_folded_stays_inside_screen_vertically(self):
        _x, y = ws.folded_geometry(self.AREA, 1492, 1050, 400, 144)
        self.assertLessEqual(y + ws.HANDLE_H, 1080)
        _x, y2 = ws.folded_geometry(self.AREA, 1492, -50, 400, 144)
        self.assertGreaterEqual(y2, 0)

    def test_expanded_returns_to_the_edge_it_was_folded_to(self):
        # 在右边缘收起（细条贴右边），展开后右边缘仍要对齐
        x, _y = ws.expanded_geometry(self.AREA, 1920 - ws.HANDLE_W, 300, 400, 144)
        self.assertEqual(x, 1920 - 400)

    def test_expanded_stays_inside_screen_vertically(self):
        _x, y = ws.expanded_geometry(self.AREA, 1900, 1000, 400, 400)
        self.assertLessEqual(y + 400, 1080)
        _x, y2 = ws.expanded_geometry(self.AREA, 1900, 0, 400, 400)
        self.assertGreaterEqual(y2, 0)


class GroupingTest(unittest.TestCase):
    def test_groups_ordered_by_recent_activity(self):
        sessions = [
            make_session("a", "alpha", last_active=100),
            make_session("b", "beta", last_active=900),
            make_session("c", "alpha", last_active=500),
        ]
        groups = ws.group_sessions(sessions, [])
        self.assertEqual([g["project"] for g in groups], ["beta", "alpha"])
        alpha = next(g for g in groups if g["project"] == "alpha")
        self.assertEqual([s["composerId"] for s in alpha["sessions"]], ["c", "a"])

    def test_collapsed_flag_and_warn(self):
        sessions = [
            make_session("a", "alpha"),
            make_session("b", "beta", state="waiting"),
        ]
        groups = ws.group_sessions(sessions, ["alpha"])
        by_name = {g["project"]: g for g in groups}
        self.assertTrue(by_name["alpha"]["collapsed"])
        self.assertFalse(by_name["alpha"]["warn"])
        self.assertTrue(by_name["beta"]["warn"])

    def test_session_dict_has_page_fields(self):
        groups = ws.group_sessions([make_session("a", "alpha")], [])
        item = groups[0]["sessions"][0]
        for key in ("composerId", "title", "state", "action", "detail",
                    "elapsedSec", "isSubagent", "project"):
            self.assertIn(key, item)


class SignatureTest(unittest.TestCase):
    def test_unchanged_content_has_same_signature(self):
        one = [make_session("a", elapsed=10.0)]
        two = [make_session("a", elapsed=12.0)]      # 同一档（//10）
        self.assertEqual(ws.signature(one, [], ""), ws.signature(two, [], ""))

    def test_content_change_changes_signature(self):
        one = [make_session("a", detail="npm run build")]
        two = [make_session("a", detail="npm run test")]
        self.assertNotEqual(ws.signature(one, [], ""), ws.signature(two, [], ""))

    def test_collapse_and_error_change_signature(self):
        sessions = [make_session("a", project="alpha")]
        base = ws.signature(sessions, [], "")
        self.assertNotEqual(base, ws.signature(sessions, ["alpha"], ""))
        self.assertNotEqual(base, ws.signature(sessions, [], "boom"))


class PayloadTest(unittest.TestCase):
    def test_shape(self):
        sessions = [make_session("a", "alpha"), make_session("b", "beta", state="waiting")]
        data = ws.payload(sessions, ["beta"], {"mode": "mini", "opacity": 0.7,
                                             "pinned": True}, 0.9, "12:00:00")
        self.assertEqual(data["running"], 2)
        self.assertEqual(data["waiting"], 1)
        self.assertEqual(data["settings"]["mode"], "mini")
        self.assertEqual(data["intervalSec"], "0.9")
        self.assertEqual(len(data["groups"]), 2)
        self.assertTrue(data["groups"][0]["collapsed"] is not None)
        self.assertEqual(data["error"], "")

    def test_error_is_carried_through(self):
        data = ws.payload([], [], {}, 1.0, "12:00:00", error="boom")
        self.assertEqual(data["error"], "boom")
        self.assertEqual(data["running"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
