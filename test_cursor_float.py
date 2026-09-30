#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""cursor_float 的判定逻辑测试：用临时构造的 state.vscdb 模拟各种会话状态。

运行：python test_cursor_float.py
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import unittest
import uuid
from pathlib import Path

import cursor_float as cf

SCHEMA = """
CREATE TABLE composerHeaders (
    composerId TEXT PRIMARY KEY, workspaceId TEXT, createdAt INTEGER,
    lastUpdatedAt INTEGER, isArchived INTEGER, isSubagent INTEGER,
    recency INTEGER, checkpointAt INTEGER, value TEXT, subagentTypeName TEXT
);
CREATE TABLE cursorDiskKV (key TEXT UNIQUE ON CONFLICT REPLACE, value BLOB);
"""

NOW = 1_790_000_000_000          # 固定"当前时间"，保证测试可复现
WS = {"$mid": 1, "fsPath": r"c:\work\web-app"}


def header(name, path=WS, blocking=False, workspace_id="ws1", subagent=False):
    return json.dumps({
        "name": name,
        "workspaceIdentifier": {"id": "ws", "uri": path},
        "unifiedMode": "agent",
        "isSubagent": subagent,
        "hasBlockingPendingActions": blocking,
    }, ensure_ascii=False)


def composer(cid, status="completed", generating=None, subtitle="", todos=None,
             last_bubble=None, worktree=False):
    return json.dumps({
        "composerId": cid,
        "status": status,
        "generatingBubbleIds": generating or [],
        "subtitle": subtitle,
        "todos": todos or [],
        "fullConversationHeadersOnly": ([{"bubbleId": last_bubble, "type": 2}]
                                        if last_bubble else []),
        "isApplyingWorktree": worktree,
        "unifiedMode": "agent",
    }, ensure_ascii=False)


def bubble(name=None, status="completed", params="", started_ms=NOW - 83_000):
    b = {"bubbleId": "b1", "type": 2}
    if name:
        b["toolFormerData"] = {
            "name": name, "status": status, "params": params,
            "additionalData": {"status": "pending" if status == "running" else "success",
                               "startedAtMs": started_ms},
        }
    return json.dumps(b, ensure_ascii=False)


TMP_ROOT = Path(__file__).resolve().parent / ".tmp-test"


class DetectionTest(unittest.TestCase):
    def setUp(self):
        # 不用 tempfile.mkdtemp：它创建的目录带 owner-only ACL，
        # 在受限沙箱下会导致后续访问被拒绝。
        TMP_ROOT.mkdir(exist_ok=True)
        self.tmpdir = TMP_ROOT / f"case-{uuid.uuid4().hex[:8]}"
        self.tmpdir.mkdir()
        self.db = self.tmpdir / "state.vscdb"
        con = sqlite3.connect(str(self.db))
        con.executescript(SCHEMA)
        self.con = con
        self.addCleanup(lambda: shutil.rmtree(self.tmpdir, ignore_errors=True))
        self.addCleanup(con.close)
        self.cfg = {**cf.DEFAULTS, "header_scan": 50, "live_scan": 50}

    def put(self, cid, name, *, archived=0, updated=NOW, subagent=0, blocking=False,
            cdata=None, bub=None, path=WS):
        self.con.execute(
            "insert into composerHeaders values (?,?,?,?,?,?,?,?,?,?)",
            (cid, "ws1", NOW - 600_000, updated, archived, subagent, 0, updated,
             header(name, path, blocking, subagent=bool(subagent)), "generalPurpose"))
        if cdata is not None:
            self.con.execute("insert into cursorDiskKV values (?,?)",
                             (f"composerData:{cid}", cdata))
        if bub is not None:
            self.con.execute("insert into cursorDiskKV values (?,?)",
                             (f"bubbleId:{cid}:b1", bub))
        self.con.commit()

    def snap(self):
        return {s.composer_id: s for s in cf.CursorReader(self.db).snapshot(self.cfg, now_ms=NOW)}

    def snap_default(self):
        """用接近真实默认值的配置快照（setUp 里的 live_scan=50 会掩盖筛选问题）。"""
        cfg = {**self.cfg, "live_scan": cf.DEFAULTS["live_scan"]}
        return {s.composer_id: s for s in cf.CursorReader(self.db).snapshot(cfg, now_ms=NOW)}

    # -- 真实生效的活跃信号 -------------------------------------------------
    # 以下用例复现真实库上采样到的行为：生成期间 composerData 里的 status 恒为
    # 上一次落盘的值（常常是 "aborted"）、generatingBubbleIds 恒为空、头部时间戳
    # 全部冻住。只有内容本身在变。

    def test_generating_with_stale_aborted_status_is_detected(self):
        """status='aborted' + 空 generatingBubbleIds + 冻住的头部时间戳，
        只要 composerData 的内容在变，就必须认出来。"""
        ws = {"$mid": 1, "fsPath": r"d:\workspace\cursor\test"}
        self.put("live", "长任务示例", updated=NOW - 200_000, path=ws,
                 cdata=composer("live", status="aborted", generating=[], last_bubble="b1"),
                 bub=bubble())
        reader = cf.CursorReader(self.db)
        cfg = {**self.cfg, "live_scan": 8}

        reader.snapshot(cfg, now_ms=NOW)                      # 第一次建立指纹
        # 内容推进：多了一条气泡
        self.con.execute("update cursorDiskKV set value=? where key='composerData:live'",
                         (composer("live", status="aborted", generating=[], last_bubble="b2"),))
        self.con.execute("insert into cursorDiskKV values (?,?)",
                         ("bubbleId:live:b2", bubble()))
        self.con.commit()

        got = reader.snapshot(cfg, now_ms=NOW + 2000)
        self.assertEqual([s.composer_id for s in got], ["live"],
                         "内容在变就说明在跑，不能因为 status 过期而漏掉")
        self.assertIn(got[0].action_label, ("生成回复中", "调用工具"))

    def test_loading_tool_status_counts_as_running(self):
        """Cursor 对正在执行的工具写的是 status='loading'。"""
        self.put("ld", "磁盘占用排查", updated=NOW - 300_000,
                 cdata=composer("ld", status="aborted", last_bubble="b1"),
                 bub=bubble("run_terminal_command_v2", "loading", '{"command":"du -sh /d"}'))
        got = self.snap_default()
        self.assertIn("ld", got, "loading 是在执行的工具，必须认")
        self.assertEqual(got["ld"].action_label, "执行命令")
        self.assertEqual(got["ld"].action_detail, "du -sh /d")
        self.assertTrue(any("tool=running" in s for s in got["ld"].signals))

    def test_completed_tool_is_not_running(self):
        """已结束的工具不能算在跑。"""
        self.put("fin", "早就跑完的", updated=NOW - 300_000,
                 cdata=composer("fin", status="completed", last_bubble="b1"),
                 bub=bubble("run_terminal_command_v2", "completed",
                            '{"command":"ls"}', started_ms=NOW - 300_000))
        self.assertNotIn("fin", self.snap_default())

    def test_session_drops_out_after_change_stops(self):
        """停止变化后超过 activity_grace 秒就该从窗口消失。"""
        self.put("st", "跑完的任务", updated=NOW - 300_000,
                 cdata=composer("st", status="aborted", generating=[], last_bubble="b1"),
                 bub=bubble())
        reader = cf.CursorReader(self.db)
        cfg = {**self.cfg, "live_scan": 8, "activity_grace_sec": 45}

        reader.snapshot(cfg, now_ms=NOW)
        self.con.execute("update cursorDiskKV set value=? where key='composerData:st'",
                         (composer("st", status="aborted", generating=[], last_bubble="b2"),))
        self.con.execute("insert into cursorDiskKV values (?,?)",
                         ("bubbleId:st:b2", bubble()))
        self.con.commit()

        # 刚变化完 -> 还在
        self.assertIn("st", {s.composer_id for s in reader.snapshot(cfg, now_ms=NOW + 3000)})
        # 超过宽限期仍未再变化 -> 消失
        later = NOW + 3000 + 60_000
        self.assertNotIn("st", {s.composer_id for s in reader.snapshot(cfg, now_ms=later)})

    def test_composer_parse_cache_avoids_reparsing(self):
        """blob 没变时应复用上次解析结果，不重复 json.loads。"""
        self.put("cc", "缓存", updated=NOW - 300_000,
                 cdata=composer("cc", status="aborted", last_bubble="b1"), bub=bubble())
        reader = cf.CursorReader(self.db)
        cfg = {**self.cfg, "live_scan": 8}
        reader.snapshot(cfg, now_ms=NOW)
        cached = reader._cache.get("cc")
        self.assertIsNotNone(cached)
        parsed_obj = cached[1]
        reader.snapshot(cfg, now_ms=NOW + 500)
        self.assertIs(reader._cache["cc"][1], parsed_obj,
                      "blob 未变化时不应重新解析")

    def test_two_projects_running_at_once_both_shown(self):
        """两个不同项目的任务同时在跑，两个都要出现，且各自的当前动作要对。"""
        ws_a = {"$mid": 1, "fsPath": r"c:\work\project-alpha"}
        ws_b = {"$mid": 1, "fsPath": r"c:\work\project-beta"}
        self.put("pa", "alpha 的任务", updated=NOW - 1000, path=ws_a,
                 cdata=composer("pa", status="generating", generating=["b1"], last_bubble="b1"),
                 bub=bubble("run_terminal_command_v2", "running", '{"command":"npm run dev"}'))
        self.put("pb", "beta 的任务", updated=NOW - 1000, path=ws_b,
                 cdata=composer("pb", status="generating", generating=["b1"], last_bubble="b1"),
                 bub=bubble("edit_file_v2", "running", '{"path":"src/b.ts"}'))

        got = self.snap_default()
        self.assertEqual(set(got), {"pa", "pb"}, "两个项目的任务都必须显示")
        self.assertEqual(got["pa"].project, "project-alpha")
        self.assertEqual(got["pb"].project, "project-beta")
        self.assertEqual(got["pa"].action_detail, "npm run dev")
        self.assertEqual(got["pb"].action_detail, "src/b.ts")

    def test_long_running_large_session_is_not_dropped(self):
        """长时间运行的任务：头部时间戳已过期、会话对象又很大，也一样不能丢。

        这是真实场景：agent 跑久了 composerData 会长到几 MB，而头部时间戳
        在工具执行期间并不刷新，于是它既不在「近期」窗口里、也没被跟踪过。
        """
        big = json.loads(composer("pbig", status="generating", generating=["b1"],
                                  last_bubble="b1"))
        big["codeBlockData"] = {"pad": "x" * (700 * 1024)}      # 撑过体积阈值
        self.put("pbig", "跑了很久的任务", updated=NOW - 1_200_000,   # 头部是 20 分钟前的
                 cdata=json.dumps(big, ensure_ascii=False),
                 bub=bubble("run_terminal_command_v2", "running", '{"command":"mvn package"}'))
        self.put("pnew", "刚起的新任务", updated=NOW - 500,
                 cdata=composer("pnew", status="generating", generating=["b1"]), bub=bubble())

        got = self.snap_default()
        self.assertIn("pbig", got, "长时间运行的大会话不能被体积保护误杀")
        self.assertIn("pnew", got)
        self.assertEqual(got["pbig"].action_detail, "mvn package")

    # -- 判定 ---------------------------------------------------------------
    def test_generating_is_detected(self):
        self.put("c1", "重构登录", updated=NOW - 2000,
                 cdata=composer("c1", status="generating", generating=["b1"],
                                subtitle="Read auth.ts"),
                 bub=bubble())
        s = self.snap()["c1"]
        self.assertEqual(s.state, "running")
        self.assertEqual(s.action_label, "生成回复中")
        self.assertIn("Read auth.ts", s.action_detail)

    def test_running_tool_is_detected_with_command(self):
        self.put("c2", "跑构建", updated=NOW - 500,
                 cdata=composer("c2", status="generating", generating=["b1"], last_bubble="b1"),
                 bub=bubble("run_terminal_command_v2", "running", '{"command":"npm run build"}'))
        s = self.snap()["c2"]
        self.assertEqual(s.action_label, "执行命令")
        self.assertEqual(s.action_detail, "npm run build")

    def test_edit_file_action_shows_path(self):
        self.put("c3", "改文件", updated=NOW - 500,
                 cdata=composer("c3", status="generating", generating=["b1"], last_bubble="b1"),
                 bub=bubble("edit_file_v2", "running",
                            '{"path":"src/a.ts","explanation":"fix"}'))
        s = self.snap()["c3"]
        self.assertEqual(s.action_label, "编辑文件")
        self.assertEqual(s.action_detail, "src/a.ts")

    def test_blocking_pending_actions_mark_waiting(self):
        self.put("c4", "等待批准", updated=NOW - 1000, blocking=True,
                 cdata=composer("c4", status="generating", generating=["b1"], last_bubble="b1"),
                 bub=bubble("run_terminal_command_v2", "running", '{"command":"rm -rf x"}'))
        self.assertEqual(self.snap()["c4"].state, "waiting")

    def test_todo_in_progress_becomes_action(self):
        self.put("c5", "长任务", updated=NOW - 1000,
                 cdata=composer("c5", status="generating", generating=["b1"],
                                todos=[{"status": "in_progress", "content": "迁移索引"}]),
                 bub=bubble())
        s = self.snap()["c5"]
        self.assertEqual(s.action_label, "生成回复中")
        self.assertEqual(s.action_detail, "迁移索引")

    # -- 过滤 ---------------------------------------------------------------
    def test_idle_old_session_is_hidden(self):
        self.put("c6", "早就结束了", updated=NOW - 3_600_000,
                 cdata=composer("c6", status="completed"), bub=bubble())
        self.assertNotIn("c6", self.snap())

    def test_archived_is_hidden(self):
        self.put("c7", "归档的", archived=1, updated=NOW - 500,
                 cdata=composer("c7", status="generating", generating=["b1"]), bub=bubble())
        self.assertNotIn("c7", self.snap())

    def test_grace_window_keeps_session_between_tool_calls(self):
        # 工具刚结束，头部 lastUpdatedAt 落在宽限期内 -> 仍显示，避免闪烁
        self.put("c8", "工具间隙", updated=NOW - 5_000,
                 cdata=composer("c8", status="completed", last_bubble="b1"),
                 bub=bubble("read_file_v2", "completed", '{"path":"README.md"}',
                            started_ms=NOW - 20_000))
        s = self.snap()["c8"]
        self.assertEqual(s.action_label, "读取文件")
        self.assertGreaterEqual(s.elapsed_sec, 19)

    def test_subagent_flagged(self):
        self.put("c9", "子任务", subagent=1, updated=NOW - 400,
                 cdata=composer("c9", status="generating", generating=["b1"]), bub=bubble())
        self.assertTrue(self.snap()["c9"].is_subagent)

    # -- 长时间运行 ---------------------------------------------------------
    def test_long_tool_run_survives_stale_header_timestamp(self):
        """头部时间戳不再更新（长命令执行中），只要 status 仍是 generating 就应继续显示。"""
        # 用真实的 live_scan=4 语义：这条会话不在最靠前的位置
        self.put("cx", "跑长命令", updated=NOW - 2000,
                 cdata=composer("cx", status="generating", generating=["b1"],
                                last_bubble="b1"),
                 bub=bubble("run_terminal_command_v2", "running", '{"command":"npm run build"}'))
        reader = cf.CursorReader(self.db)
        cfg = {**self.cfg, "live_scan": 0, "candidate_window_sec": 5}
        first = reader.snapshot(cfg, now_ms=NOW)
        self.assertEqual([s.composer_id for s in first], ["cx"])

        # 10 分钟后头部时间戳早已过期，超出候选窗口
        later = NOW + 600_000
        second = reader.snapshot(cfg, now_ms=later)
        self.assertEqual([s.composer_id for s in second], ["cx"],
                         "被跟踪的活跃会话不应因为时间戳变旧而消失")

    def test_pinned_session_drops_once_it_finishes(self):
        self.put("cy", "跑长命令", updated=NOW - 2000,
                 cdata=composer("cy", status="generating", generating=["b1"],
                                last_bubble="b1"),
                 bub=bubble("run_terminal_command_v2", "running", '{"command":"build"}'))
        reader = cf.CursorReader(self.db)
        cfg = {**self.cfg, "live_scan": 0, "candidate_window_sec": 5}
        self.assertEqual(len(reader.snapshot(cfg, now_ms=NOW)), 1)

        # 会话结束：status 变 completed，工具也跑完了
        self.con.execute("update cursorDiskKV set value=? where key='composerData:cy'",
                         (composer("cy", status="completed", last_bubble="b1"),))
        self.con.execute("update cursorDiskKV set value=? where key='bubbleId:cy:b1'",
                         (bubble("run_terminal_command_v2", "completed",
                                 '{"command":"build"}', started_ms=NOW),))
        self.con.commit()
        self.assertEqual(reader.snapshot(cfg, now_ms=NOW + 600_000), [],
                         "已结束的会话应被移出窗口")

    # -- 文本与路径处理 -----------------------------------------------------
    def test_project_label_from_worktree_path(self):
        self.put("c10", "worktree 任务", updated=NOW - 400,
                 path={"$mid": 1,
                       "fsPath": r"c:\work\my-project\.worktrees\feat-new-api"},
                 cdata=composer("c10", status="generating", generating=["b1"]), bub=bubble())
        self.assertEqual(self.snap()["c10"].project, "my-project · feat-new-api")

    def test_file_uri_decoding(self):
        self.assertEqual(
            cf._decode_file_uri("file:///c%3A/work/web-app"),
            r"c:\work\web-app")

    def test_elapsed_formatting(self):
        self.assertEqual(cf.fmt_elapsed(45), "45s")
        self.assertEqual(cf.fmt_elapsed(83), "1m23s")
        self.assertEqual(cf.fmt_elapsed(3725), "1h02m")

    def test_tool_detail_falls_back_to_raw_params(self):
        self.assertEqual(cf._tool_detail('{"unknownKey":"some value"}'), ("", "some value"))

    def test_json_snapshot_is_serialisable(self):
        self.put("c11", "序列化", updated=NOW - 400,
                 cdata=composer("c11", status="generating", generating=["b1"]), bub=bubble())
        payload = [cf.session_to_dict(s) for s in
                   cf.CursorReader(self.db).snapshot(self.cfg, now_ms=NOW)]
        json.dumps(payload, ensure_ascii=False)


if __name__ == "__main__":
    unittest.main(verbosity=2)
