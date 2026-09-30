#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cursor-float —— 悬浮窗显示 Cursor 中「正在运行」的 Agent 会话及当前动作。

数据来源（全部只读，不写入 Cursor 任何文件）：
  %APPDATA%\\Cursor\\User\\globalStorage\\state.vscdb
    - composerHeaders                    每个会话一行（标题/工作区/最后活动/是否归档/子代理）
    - cursorDiskKV['composerData:<id>']   会话实时状态（status / generatingBubbleIds / 待办）
    - cursorDiskKV['bubbleId:<id>:<bid>'] 最近一条气泡，toolFormerData 给出「当前在跑什么工具」

运行：
    python cursor_float.py                # 打开悬浮窗
    python cursor_float.py --watch        # 终端滚动输出（调试用）
    python cursor_float.py --once         # 打印一次快照后退出
    python cursor_float.py --json         # 输出 JSON 快照
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import threading
import time
import queue
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

# --------------------------------------------------------------------------
# 默认配置（可被脚本同目录的 config.json 覆盖）
# --------------------------------------------------------------------------

DEFAULTS: dict[str, Any] = {
    "interval_ms": 900,        # 轮询间隔
    "grace_seconds": 25,       # 最后一次活动在该秒数内仍视为「运行中」，用于衔接工具调用之间的空档
    "header_scan": 60,         # 读取最近多少条 composerHeaders
    "live_scan": 8,            # 无论时间戳如何，都强制检查的最靠前会话数（兜底）
    "first_scan": 12,          # 启动后第一轮最多检查多少个会话（避免首屏卡顿）
    "candidate_window_sec": 900,  # 头部时间戳在这个窗口内的会话才去解析状态对象
    "track_keep_sec": 600,     # 曾经活跃过的会话，继续检查多久
    "activity_grace_sec": 45,  # 观察到内容变化后，还视为「运行中」多久
    "max_rows": 8,             # 窗口最多显示几条会话
    "width": 400,              # 窗口宽度
    "alpha": 0.96,             # 窗口不透明度
    "topmost": True,           # 置顶
    "rounded": True,           # 圆角
}

SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = SCRIPT_DIR / "config.json"
STATE_PATH = SCRIPT_DIR / "window.json"

# 颜色
CARD_BG = "#1b1d21"
BORDER = "#31353c"
TXT = "#e7e9ec"
TXT_DIM = "#9aa1ab"
TXT_FAINT = "#71787f"
GREEN = "#3ecf72"
AMBER = "#e5a53a"
BLUE = "#4c9aff"
KEY_COLOR = "#010203"          # 透明色键，勿用于任何可见控件

SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

# 工具名 -> (中文动作, 参数里取哪个字段作为详情)
TOOL_MAP: dict[str, tuple[str, str]] = {
    "run_terminal_command_v2": ("执行命令", "command"),
    "run_terminal_command": ("执行命令", "command"),
    "edit_file_v2": ("编辑文件", "path"),
    "edit_file": ("编辑文件", "path"),
    "apply_patch": ("应用补丁", "path"),
    "read_file_v2": ("读取文件", "path"),
    "read_file": ("读取文件", "path"),
    "list_dir_v2": ("列出目录", "path"),
    "list_dir": ("列出目录", "path"),
    "glob_file_search": ("查找文件", "globPattern"),
    "file_search": ("查找文件", "query"),
    "grep_search_v2": ("搜索代码", "pattern"),
    "grep_search": ("搜索代码", "pattern"),
    "codebase_search": ("语义搜索", "query"),
    "semantic_search": ("语义搜索", "query"),
    "web_search": ("联网搜索", "searchTerm"),
    "web_fetch": ("抓取网页", "url"),
    "delete_file": ("删除文件", "path"),
    "create_file": ("新建文件", "path"),
    "todo_write": ("更新任务列表", "summary"),
    "update_todos": ("更新任务列表", "summary"),
    "run_terminal_cmd": ("执行命令", "command"),
}

DETAIL_KEYS = (
    "command", "path", "relativeWorkspacePath", "targetFile", "filePath",
    "pattern", "query", "searchTerm", "url", "globPattern", "summary", "explanation",
)


# --------------------------------------------------------------------------
# 数据层
# --------------------------------------------------------------------------

@dataclass
class Session:
    composer_id: str
    project: str
    title: str
    state: str                 # running | waiting
    action_label: str
    action_detail: str
    elapsed_sec: float
    is_subagent: bool = False
    subagent_type: str = ""
    mode: str = ""
    last_active_ms: int = 0        # 头部最后活动时间，用于给项目分组排序
    signals: list[str] = field(default_factory=list)

    @property
    def key(self) -> str:
        return self.composer_id


def _cursor_paths() -> tuple[Path, Path]:
    appdata = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    base = Path(appdata) / "Cursor" / "User"
    return base / "globalStorage" / "state.vscdb", base / "workspaceStorage"


DB_PATH, WS_ROOT = _cursor_paths()


def load_config() -> dict[str, Any]:
    cfg = dict(DEFAULTS)
    if CONFIG_PATH.exists():
        try:
            cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
        except Exception:
            pass
    return cfg


def _json_or_none(raw: Any) -> Optional[dict]:
    if raw is None:
        return None
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", "replace")
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        val = json.loads(raw)
    except Exception:
        return None
    return val if isinstance(val, dict) else None


def _decode_file_uri(uri: Any) -> str:
    """从 Cursor 存的各种 uri 结构里取出本地路径。"""
    if isinstance(uri, str):
        s = uri
    elif isinstance(uri, dict):
        s = uri.get("fsPath") or uri.get("path") or uri.get("external") or ""
    else:
        return ""
    if not s:
        return ""
    if s.startswith("file://"):
        from urllib.parse import unquote, urlparse
        p = urlparse(s)
        s = unquote((p.netloc + p.path) if p.netloc else p.path)
        if len(s) > 2 and s[0] == "/" and s[2] == ":":
            s = s[1:]
    return s.replace("/", "\\")


def project_label(path: str, fallback: str) -> str:
    if not path:
        return fallback
    p = path.rstrip("\\/")
    # worktree 形如 <repo>\.worktrees\<name>，显示为 "repo · worktree name"
    low = p.lower()
    marker = "\\.worktrees\\"
    idx = low.rfind(marker)
    if idx != -1:
        repo = Path(p[:idx]).name
        wt = Path(p[idx + len(marker):]).name
        return f"{repo} · {wt}"
    return Path(p).name or fallback


def _tool_detail(params: Any) -> tuple[str, str]:
    p = _json_or_none(params) if not isinstance(params, dict) else params
    if p is None:
        if isinstance(params, str):
            return "", params.strip().replace("\n", " ")[:160]
        return "", ""
    for k in DETAIL_KEYS:
        v = p.get(k)
        if isinstance(v, str) and v.strip():
            return k, " ".join(v.split())[:160]
    for v in p.values():                      # 兜底：第一个像样的字符串
        if isinstance(v, str) and v.strip():
            return "", " ".join(v.split())[:160]
    return "", ""


def _describe_tool(tfd: Optional[dict], bubble: Optional[dict]) -> tuple[str, str, bool]:
    """返回 (动作中文, 详情, 该工具是否正在执行)。

    Cursor 对「正在执行」的工具写的是 status="loading"（实测旧库里 40 个已结束
    的会话最后一条气泡要么没有工具、要么是 "completed"，只有真正在跑的那个是
    "loading"），所以这里必须认它。
    """
    if tfd:
        name = (tfd.get("name") or "").strip()
        label, _ = TOOL_MAP.get(name, ("", ""))
        _, detail = _tool_detail(tfd.get("params"))
        st = (tfd.get("status") or "").lower()
        add = tfd.get("additionalData") if isinstance(tfd.get("additionalData"), dict) else {}
        add_st = str(add.get("status") or "").lower()
        running = st in ("loading", "running", "pending", "in_progress") \
            or add_st in ("loading", "pending", "running")
        if not label:
            label = name.replace("_", " ").strip() or "调用工具"
        return label, detail, running
    return "", "", False


def _elapsed_ms_from(tfd: Optional[dict], bubble: Optional[dict], fallback_ms: int) -> int:
    if tfd:
        add = tfd.get("additionalData")
        if isinstance(add, dict) and add.get("startedAtMs"):
            return int(add["startedAtMs"])
    if bubble and bubble.get("startedAtMs"):
        return int(bubble["startedAtMs"])
    return fallback_ms


class CursorReader:
    """只读读取 Cursor 会话状态。绝不对 state.vscdb 做任何写操作。"""

    def __init__(self, db_path: Path = DB_PATH) -> None:
        self.db_path = db_path
        # 会话内容指纹 -> 上一次轮询的值。真正的「在跑」证据是内容在变，
        # 而不是 composerData 里的 status 字段（实测那个字段在生成期间不刷新）。
        self._fp: dict[str, tuple] = {}
        # 会话 -> 最近一次观察到内容变化的时间（time.monotonic）
        self._active: dict[str, float] = {}
        # 会话 -> 最近一次被判定为运行中的时间，用来决定还值不值得继续检查
        self._tracked: dict[str, float] = {}
        # composerData 解析缓存：cid -> (内容摘要, 解析结果)
        self._cache: dict[str, tuple[tuple[int, int], Optional[dict]]] = {}
        # 最近一次轮询对每个会话的判断依据，--watch --signals 用它解释「为什么没显示」
        self.last_probe: list[dict[str, Any]] = []
        self._probe_info: dict[str, dict[str, Any]] = {}

    def connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(
            f"file:{self.db_path}?mode=ro", uri=True, timeout=2.0, check_same_thread=False
        )
        con.row_factory = None
        return con

    def available(self) -> bool:
        return self.db_path.exists()

    # -- 单次轮询：返回当前所有「运行中」的会话 --------------------------------
    def snapshot(self, cfg: dict[str, Any], now_ms: Optional[int] = None) -> list[Session]:
        now_ms = now_ms or int(time.time() * 1000)
        con = self.connect()
        try:
            cur = con.cursor()
            headers = cur.execute(
                "select composerId, workspaceId, lastUpdatedAt, isSubagent, "
                "subagentTypeName, value, checkpointAt from composerHeaders "
                "where isArchived = 0 order by lastUpdatedAt desc limit ?",
                (int(cfg["header_scan"]),),
            ).fetchall()

            out: list[Session] = []
            always_n = int(cfg["live_scan"])
            first_n = int(cfg.get("first_scan", always_n))
            grace_ms = float(cfg["grace_seconds"]) * 1000.0
            cand_ms = float(cfg["candidate_window_sec"]) * 1000.0
            keep_ms = float(cfg["track_keep_sec"]) * 1000.0
            act_ms = float(cfg["activity_grace_sec"]) * 1000.0
            seen: set[str] = set()
            self._probe_info = {}
            probe: list[dict[str, Any]] = []

            for idx, (cid, wid, lu, is_sub, sub_type, hval, ckat) in enumerate(headers):
                if not cid:
                    continue
                lu = int(lu or 0)
                ckat = int(ckat or 0)
                seen.add(cid)
                # 注意：这里不能用头部时间戳判断「是否在跑」。实测一个正在生成的
                # 会话，lastUpdatedAt 会冻在回合开始那一刻，checkpointAt 也不是心跳。
                # 头部时间戳只用来决定「还值不值得去看一眼」。
                fresh = (now_ms - max(lu, ckat)) <= cand_ms
                tracked = (now_ms - self._tracked.get(cid, 0)) <= keep_ms
                first_sight = cid not in self._fp
                # 首次启动时值得多看几个（可能有会话正跑在长任务里、头部时间戳早就冻住了），
                # 但不能无上限——否则第一轮会把所有历史会话的几 MB 对象全解析一遍。
                examined = bool(fresh or tracked or idx < always_n
                                or (first_sight and idx < first_n))
                if not examined:
                    probe.append({"cid": cid, "examined": False,
                                  "why": "头部时间戳太旧且不在最近列表里", "live": False})
                    continue
                cdata = self._composer(cur, cid)
                sess = self._classify(
                    cur, cid, wid, lu, bool(is_sub), sub_type or "", hval,
                    cdata, now_ms, grace_ms, act_ms, tracked, first_sight,
                )
                item = self._probe_info.get(cid, {})
                item.update({"cid": cid, "examined": True, "live": sess is not None,
                             "idx": idx, "fresh": fresh, "tracked": tracked,
                             "first_sight": first_sight,
                             "head_age_s": round((now_ms - lu) / 1000.0, 1)})
                probe.append(item)
                if sess is not None:
                    self._tracked[cid] = now_ms
                    out.append(sess)

            self.last_probe = probe
            self._prune(seen)
            return out
        finally:
            con.close()

    def _prune(self, seen: set[str]) -> None:
        """防止这些按会话累积的字典无限增长。"""
        if len(self._fp) > 512:
            self._fp = {k: v for k, v in self._fp.items() if k in seen}
            self._active = {k: v for k, v in self._active.items() if k in seen}
            self._cache = {k: v for k, v in self._cache.items() if k in seen}
        if len(self._tracked) > 256:
            self._tracked = {k: v for k, v in self._tracked.items() if k in seen}

    # -- 内部：点查 ----------------------------------------------------------
    def _composer(self, cur: sqlite3.Cursor, cid: str) -> Optional[dict]:
        """读取 composerData，并复用未变化内容的解析结果。

        单个 composerData 可达数 MB，json 解析是这里最大的开销（实测 3.9 MB
        约 25 ms），而取出原始字节本身很快。所以这里用「长度 + 内容摘要」判断
        是否真的变了：变了才重新解析。

        不要退化成只比长度：内容更新后长度有可能恰好不变，那样就会把一次真实
        更新误判成没变化（这正是单元测试 test_generating_with_stale_aborted_
        status_is_detected 覆盖的场景）。
        """
        key = f"composerData:{cid}"
        try:
            row = cur.execute(
                "select value from cursorDiskKV where key = ?", (key,)
            ).fetchone()
        except sqlite3.Error:
            return None
        if not row or row[0] is None:
            self._cache.pop(cid, None)
            return None
        raw = row[0]
        if isinstance(raw, str):
            raw = raw.encode("utf-8", "replace")
        digest = (len(raw), hash(raw))
        hit = self._cache.get(cid)
        if hit and hit[0] == digest:
            return hit[1]
        parsed = _json_or_none(raw)
        self._cache[cid] = (digest, parsed)
        return parsed

    @staticmethod
    def _bubble(cur: sqlite3.Cursor, cid: str, bid: str) -> Optional[dict]:
        try:
            row = cur.execute(
                "select value from cursorDiskKV where key = ?", (f"bubbleId:{cid}:{bid}",)
            ).fetchone()
        except sqlite3.Error:
            return None
        return _json_or_none(row[0]) if row else None

    def _classify(
        self, cur, cid, wid, lu, is_sub, sub_type, hval, cdata, now_ms, grace_ms,
        activity_grace_ms: float = 45000.0, tracked: bool = False,
        first_sight: bool = False,
    ) -> Optional[Session]:
        hdr = _json_or_none(hval) or {}

        # 项目名
        path = ""
        for src in (hdr.get("workspaceIdentifier"), hdr.get("agentLocation")):
            if isinstance(src, dict):
                if "uri" in src:
                    path = _decode_file_uri(src.get("uri"))
                elif "environment" in src and isinstance(src["environment"], dict):
                    path = _decode_file_uri(src["environment"].get("uri"))
            if path:
                break
        if not path:
            path = _decode_file_uri(hdr.get("trackedGitRepos", [{}])[0].get("repoPath")
                                    if hdr.get("trackedGitRepos") else "")
        project = project_label(path, "空窗口" if wid == "empty-window" else (wid or "未知工作区")[:8])
        title = hdr.get("name") or (cdata or {}).get("name") or "（未命名会话）"
        mode = hdr.get("unifiedMode") or (cdata or {}).get("unifiedMode") or ""

        # --- 第一步：判断这个会话是不是真的在跑 -----------------------------
        # 实测结论（在真实库上采样确认）：生成期间
        #   status 恒为上一次落盘的值（可能是 aborted）、generatingBubbleIds 恒为空、
        #   lastUpdatedAt / checkpointAt 都不刷新。
        # 唯一可靠的证据是 composerData 的内容本身在变化 —— 气泡数在涨、
        # 最后一条气泡的 id 在换、blob 长度在变。
        signals: list[str] = []
        hdrs = (cdata or {}).get("fullConversationHeadersOnly") or []
        last = hdrs[-1] if hdrs and isinstance(hdrs[-1], dict) else {}
        fp = (len(hdrs), last.get("bubbleId")) if cdata is not None else None

        changed = False
        if fp is not None:
            prev = self._fp.get(cid)
            if prev is not None and prev != fp:
                changed = True
                self._active[cid] = now_ms
            self._fp[cid] = fp
        last_change = self._active.get(cid, 0)
        active_recent = bool(last_change) and (now_ms - last_change) <= activity_grace_ms

        gen_ids: list[str] = []
        status = ""
        doing = ""
        legacy_live = False
        if cdata is not None:
            gen_ids = cdata.get("generatingBubbleIds") or []
            status = str(cdata.get("status") or "").lower()
            if gen_ids:
                signals.append("generatingBubbleIds")
                legacy_live = True
            if status and status not in ("completed", "aborted", "none"):
                signals.append(f"status={status}")
                legacy_live = True
            for k in ("isApplyingWorktree", "isCreatingWorktree", "isReadingLongFile",
                      "isUndoingWorktree", "pendingCreateWorktree"):
                if cdata.get(k):
                    signals.append(k)
                    legacy_live = True
            for t in (cdata.get("todos") or []):
                if isinstance(t, dict) and str(t.get("status", "")).lower() in ("in_progress", "in-progress"):
                    doing = (t.get("content") or t.get("title") or "").strip()
                    break

        if changed:
            signals.append("内容有变化")
        elif active_recent:
            signals.append(f"变化于{int((now_ms - last_change) / 1000)}秒前")

        blocking = bool(hdr.get("hasBlockingPendingActions"))
        header_recent = (now_ms - lu) <= grace_ms

        # --- 第二步：值得看的才去读最后一条气泡（拿「当前动作」）-------------
        # 内容没变、又不在活跃期、也没别的活跃线索时，不必读气泡。
        probe_bubble = (changed or active_recent or legacy_live or blocking
                        or header_recent or first_sight or tracked)
        bubble: Optional[dict] = None
        tfd: Optional[dict] = None
        action_label = action_detail = ""
        tool_running = False
        if cdata is not None and probe_bubble:
            last_bid = last.get("bubbleId")
            bubble = self._bubble(cur, cid, last_bid) if last_bid else None
            tfd = bubble.get("toolFormerData") if isinstance(bubble, dict) else None
            label, detail, tool_running = _describe_tool(tfd, bubble)
            if tool_running:
                signals.append("tool=running")

            if tool_running:
                action_label = label or "调用工具"
                action_detail = detail
            elif gen_ids or changed or active_recent:
                action_label = "生成回复中"
                action_detail = doing or (cdata.get("subtitle") or "")
            elif doing:
                action_label = "执行任务"
                action_detail = doing
            elif label:
                action_label = label
                action_detail = detail

        live = (changed or active_recent or legacy_live or tool_running
                or blocking or header_recent)
        # 记录判断依据，供 --watch --signals 解释「为什么显示 / 为什么不显示」
        self._probe_info[cid] = {
            "title": str(title)[:44],
            "project": project,
            "status": status or "-",
            "bubbles": len(hdrs),
            "gen": len(gen_ids),
            "changed": changed,
            "active_recent": active_recent,
            "tool_running": tool_running,
            "legacy_live": legacy_live,
            "blocking": blocking,
            "header_recent": header_recent,
            "why": "命中活跃信号" if live else "没有任何活跃证据（内容没变、工具没在跑）",
        }
        if not live:
            return None

        if blocking:
            signals.append("hasBlockingPendingActions")
        if header_recent:
            signals.append(f"头部活动于{int((now_ms - lu) / 1000)}秒前")

        state = "waiting" if blocking else "running"

        # 计时基准：优先用该工具/回复的开始时间
        base_ms = int(hdr.get("lastUpdatedAt") or lu or now_ms)
        base_ms = _elapsed_ms_from(tfd, bubble, base_ms)
        elapsed = max(0.0, (now_ms - base_ms) / 1000.0)

        return Session(
            composer_id=cid,
            project=project,
            title=str(title)[:80],
            state=state,
            action_label=action_label,
            action_detail=action_detail,
            elapsed_sec=elapsed,
            is_subagent=is_sub,
            subagent_type=sub_type or "",
            mode=mode,
            last_active_ms=lu,
            signals=signals,
        )


# --------------------------------------------------------------------------
# 控制台输出
# --------------------------------------------------------------------------

def fmt_elapsed(sec: float) -> str:
    sec = int(sec)
    if sec < 60:
        return f"{sec}s"
    if sec < 3600:
        return f"{sec // 60}m{sec % 60:02d}s"
    return f"{sec // 3600}h{(sec % 3600) // 60:02d}m"


def render_text(sessions: list[Session], err: str = "",
                probe: Optional[list[dict]] = None) -> str:
    if err:
        return f"[cursor-float] 读取失败: {err}"
    lines: list[str] = []
    if not sessions:
        lines.append("[cursor-float] 暂无运行中的 Cursor 会话")
    else:
        lines.append(f"[cursor-float] 运行中 {len(sessions)} 个会话")
        for s in sessions:
            flag = "◐" if s.state == "waiting" else "●"
            sub = f" [子代理:{s.subagent_type}]" if s.is_subagent else ""
            lines.append(f"  {flag} {s.project} / {s.title}{sub}")
            if s.action_label:
                lines.append(f"      → {s.action_label}: {s.action_detail}")
            lines.append(f"      ⏱ {fmt_elapsed(s.elapsed_sec)}   信号: {', '.join(s.signals)}")

    if probe is not None:
        lines.append("  --- 本次检查过的会话（判断依据）---")
        for p in probe:
            if not p.get("examined"):
                lines.append(f"  · {str(p.get('cid'))[:8]}  跳过：{p.get('why')}")
                continue
            mark = "显示" if p.get("live") else "不显示"
            lines.append(
                f"  · {str(p.get('cid'))[:8]} [{mark}] {p.get('project','?')[:28]} / "
                f"{p.get('title','?')}")
            lines.append(
                f"      status={p.get('status')} 气泡={p.get('bubbles')} "
                f"gen={p.get('gen')} 内容变化={p.get('changed')} "
                f"45秒内变化过={p.get('active_recent')} 工具在跑={p.get('tool_running')} "
                f"头部新鲜={p.get('header_recent')} (头部{p.get('head_age_s')}秒前)")
            lines.append(f"      => {p.get('why')}")
    return "\n".join(lines)


def session_to_dict(s: Session) -> dict[str, Any]:
    return {
        "composerId": s.composer_id,
        "project": s.project,
        "title": s.title,
        "state": s.state,
        "action": s.action_label,
        "detail": s.action_detail,
        "elapsedSec": round(s.elapsed_sec, 1),
        "isSubagent": s.is_subagent,
        "subagentType": s.subagent_type,
        "mode": s.mode,
        "lastActiveMs": s.last_active_ms,
        "signals": s.signals,
    }


# --------------------------------------------------------------------------
# 悬浮窗
# --------------------------------------------------------------------------

class FloatWindow:
    def __init__(self, cfg: dict[str, Any], reader: CursorReader, poll: bool = True) -> None:
        import tkinter as tk
        self.tk = tk
        self.cfg = cfg
        self.reader = reader
        self.sessions: list[Session] = []
        self.error = ""
        self.spin = 0
        self.poll = poll
        self.collapsed: set[str] = set()      # 被折叠的项目名
        self.q: queue.Queue = queue.Queue()
        self._drag = None
        self._alive = True

        self.root = tk.Tk()
        self.root.title("Cursor 运行中会话")
        self.root.overrideredirect(True)
        self._apply_window_flags()
        self.root.configure(bg=KEY_COLOR)

        self.radius = 14 if cfg.get("rounded", True) else 0
        self.canvas = tk.Canvas(self.root, bg=KEY_COLOR, highlightthickness=0, bd=0)
        self.canvas.pack(fill="both", expand=True)
        self._bg_id = None

        pad = self.radius
        self.body = tk.Frame(self.root, bg=CARD_BG)
        self.body.place(x=pad, y=pad)

        self._build_header()
        self._list = tk.Frame(self.body, bg=CARD_BG)
        self._list.pack(fill="x", padx=10, pady=(2, 10))

        self._bind_drag(self.root)
        self._bind_drag(self.canvas)

        self._restore_geometry()
        self.root.bind("<Configure>", self._redraw_bg)

        if self.poll:
            self._start_poller()
            self.root.after(60, self._drain)
        self.root.after(120, self._tick_spinner)
        self.root.protocol("WM_DELETE_WINDOW", self.quit)

    # -- 窗口外框 -----------------------------------------------------------
    def _apply_window_flags(self) -> None:
        try:
            self.root.attributes("-topmost", bool(self.cfg.get("topmost", True)))
        except Exception:
            pass
        try:
            self.root.attributes("-alpha", float(self.cfg.get("alpha", 0.96)))
        except Exception:
            pass
        if self.cfg.get("rounded", True):
            try:
                self.root.attributes("-transparentcolor", KEY_COLOR)
            except Exception:
                pass

    def _build_header(self) -> None:
        tk = self.tk
        bar = tk.Frame(self.body, bg=CARD_BG)
        bar.pack(fill="x", padx=10, pady=(8, 6))

        self._dot = tk.Canvas(bar, width=9, height=9, bg=CARD_BG, highlightthickness=0)
        self._dot.pack(side="left", padx=(2, 6))
        self._dot_id = self._dot.create_oval(1, 1, 8, 8, fill=TXT_FAINT, outline="")

        self._title = tk.Label(bar, text="Cursor", bg=CARD_BG, fg=TXT,
                               font=("Microsoft YaHei UI", 9, "bold"))
        self._title.pack(side="left")

        close = tk.Label(bar, text="✕", bg=CARD_BG, fg=TXT_FAINT,
                         font=("Segoe UI", 10), cursor="hand2")
        close.pack(side="right", padx=(6, 0))
        close.bind("<Button-1>", lambda e: self.quit())
        close.bind("<Enter>", lambda e: close.configure(fg="#ff6b6b"))
        close.bind("<Leave>", lambda e: close.configure(fg=TXT_FAINT))

        menu_btn = tk.Label(bar, text="⋯", bg=CARD_BG, fg=TXT_FAINT,
                            font=("Segoe UI", 11), cursor="hand2")
        menu_btn.pack(side="right")
        menu_btn.bind("<Button-1>", self._popup_menu)

        sep = tk.Frame(self.body, bg=BORDER, height=1)
        sep.pack(fill="x", padx=10, pady=(0, 6))

    def _popup_menu(self, event) -> None:
        tk = self.tk
        m = tk.Menu(self.root, tearoff=0)
        m.add_command(label="立即刷新", command=self._poll_now)
        if self.collapsed:
            m.add_command(label="展开全部项目", command=self._expand_all)
        projects = {s.project for s in self.sessions}
        if len(projects) > 1:
            m.add_command(label="折叠全部项目", command=self._collapse_all)
        m.add_command(label=f"{'取消' if self.cfg.get('topmost') else '开启'}置顶",
                      command=self._toggle_topmost)
        m.add_separator()
        m.add_command(label="退出", command=self.quit)
        try:
            m.tk_popup(event.x_root, event.y_root)
        finally:
            m.grab_release()

    def _expand_all(self) -> None:
        self.collapsed.clear()
        self._render()
        self._save_geometry()

    def _collapse_all(self) -> None:
        self.collapsed = {s.project for s in self.sessions}
        self._render()
        self._save_geometry()

    def _toggle_topmost(self) -> None:
        self.cfg["topmost"] = not self.cfg.get("topmost", True)
        try:
            self.root.attributes("-topmost", bool(self.cfg["topmost"]))
        except Exception:
            pass

    def _redraw_bg(self, _evt=None) -> None:
        w = self.root.winfo_width()
        h = self.root.winfo_height()
        if w <= 1 or h <= 1:
            return
        self.canvas.delete("bg")
        if self.radius > 0:
            self._bg_id = self._round_rect(0, 0, w - 1, h - 1, self.radius,
                                           fill=CARD_BG, outline=BORDER, tags="bg")
        else:
            self._bg_id = self.canvas.create_rectangle(0, 0, w - 1, h - 1,
                                                       fill=CARD_BG, outline=BORDER, tags="bg")
        self.canvas.tag_lower("bg")

    def _round_rect(self, x1, y1, x2, y2, r, **kw):
        pts = [
            x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r,
            x2, y2 - r, x2, y2, x2 - r, y2, x1 + r, y2,
            x1, y2, x1, y2 - r, x1, y1 + r, x1, y1,
        ]
        return self.canvas.create_polygon(pts, smooth=True, **kw)

    # -- 拖动 ---------------------------------------------------------------
    def _bind_drag(self, w) -> None:
        w.bind("<Button-1>", self._drag_start, add="+")
        w.bind("<B1-Motion>", self._drag_move, add="+")
        w.bind("<ButtonRelease-1>", self._drag_end, add="+")

    def _drag_start(self, e) -> None:
        self._drag = (e.x_root, e.y_root, self.root.winfo_x(), self.root.winfo_y())

    def _drag_move(self, e) -> None:
        if not self._drag:
            return
        x0, y0, wx, wy = self._drag
        self.root.geometry(f"+{wx + e.x_root - x0}+{wy + e.y_root - y0}")

    def _drag_end(self, _e) -> None:
        self._drag = None
        self._save_geometry()

    # -- 位置与折叠状态记忆 -------------------------------------------------
    def _restore_geometry(self) -> None:
        sw = self.root.winfo_screenwidth()
        x, y = sw - int(self.cfg["width"]) - 28, 60
        if STATE_PATH.exists():
            try:
                st = json.loads(STATE_PATH.read_text(encoding="utf-8"))
                x, y = int(st.get("x", x)), int(st.get("y", y))
                self.collapsed = {str(p) for p in st.get("collapsed", [])}
            except Exception:
                pass
        x = max(0, min(x, sw - 120))
        y = max(0, y)
        self.root.geometry(f"{int(self.cfg['width'])}x140+{x}+{y}")

    def _save_geometry(self) -> None:
        try:
            STATE_PATH.write_text(json.dumps({
                "x": self.root.winfo_x(),
                "y": self.root.winfo_y(),
                "collapsed": sorted(self.collapsed),
            }, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass

    # -- 轮询 ---------------------------------------------------------------
    def _start_poller(self) -> None:
        t = threading.Thread(target=self._poll_loop, daemon=True)
        t.start()

    def _poll_loop(self) -> None:
        while self._alive:
            try:
                if not self.reader.available():
                    self.q.put(("err", "找不到 state.vscdb"))
                else:
                    self.q.put(("data", self.reader.snapshot(self.cfg)))
            except Exception as exc:                       # noqa: BLE001
                self.q.put(("err", f"{type(exc).__name__}: {exc}"))
            time.sleep(max(0.2, float(self.cfg["interval_ms"]) / 1000.0))

    def _poll_now(self) -> None:
        try:
            self.q.put(("data", self.reader.snapshot(self.cfg)))
        except Exception as exc:                           # noqa: BLE001
            self.q.put(("err", str(exc)))

    def _drain(self) -> None:
        """把轮询线程的结果搬到 UI 线程，只在内容真正变化时重绘。"""
        if not self._alive:
            return
        latest = None
        err = None
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "data":
                    latest, err = payload, ""
                else:
                    err = str(payload)
        except queue.Empty:
            pass

        if err is not None:
            if err != self.error or latest is not None:
                self.error = err
                if latest is not None:
                    self.sessions = latest
                self._render()
        elif latest is not None:
            if self._signature(latest) != self._signature(self.sessions):
                self.sessions = latest
                self._render()
        self.root.after(200, self._drain)

    @staticmethod
    def _signature(sessions: list[Session]) -> tuple:
        return tuple(
            (s.key, s.state, s.action_label, s.action_detail, s.project, s.title,
             s.last_active_ms, int(s.elapsed_sec) // 10)
            for s in sessions
        )

    def _tick_spinner(self) -> None:
        if not self._alive:
            return
        self.spin = (self.spin + 1) % len(SPINNER)
        for lbl in getattr(self, "_spin_labels", []):
            try:
                lbl.configure(text=SPINNER[self.spin])
            except Exception:
                pass
        self.root.after(110, self._tick_spinner)

    # -- 渲染 ---------------------------------------------------------------
    def _group(self, sessions: list[Session]) -> list[tuple[str, list[Session]]]:
        """按项目分组，组间按最近活动时间倒序；组内按最近活动时间倒序。"""
        buckets: dict[str, list[Session]] = {}
        for s in sessions:
            buckets.setdefault(s.project, []).append(s)
        for v in buckets.values():
            v.sort(key=lambda s: -s.last_active_ms)
        return sorted(buckets.items(),
                      key=lambda kv: -max(s.last_active_ms for s in kv[1]))

    def _render(self) -> None:
        tk = self.tk
        for c in self._list.winfo_children():
            c.destroy()
        self._spin_labels = []

        running = self.sessions
        if self.error:
            self._dot.itemconfigure(self._dot_id, fill="#ff6b6b")
            self._title.configure(text="Cursor · 读取异常", fg="#ff9a9a")
            tk.Label(self._list, text=self.error, bg=CARD_BG, fg=TXT_DIM, justify="left",
                     wraplength=int(self.cfg["width"]) - 44,
                     font=("Microsoft YaHei UI", 9)).pack(anchor="w", pady=6)
        elif not running:
            self._dot.itemconfigure(self._dot_id, fill=TXT_FAINT)
            self._title.configure(text="Cursor · 空闲", fg=TXT_DIM)
            tk.Label(self._list, text="暂无运行中的会话", bg=CARD_BG, fg=TXT_DIM,
                     font=("Microsoft YaHei UI", 9)).pack(anchor="w", pady=6)
        else:
            groups = self._group(running)
            self._dot.itemconfigure(self._dot_id, fill=GREEN)
            self._title.configure(
                text=f"Cursor · 运行中 {len(running)} · {len(groups)} 个项目", fg=TXT)

            shown = 0
            overflow = 0
            limit = int(self.cfg["max_rows"])
            for gi, (project, items) in enumerate(groups):
                collapsed = project in self.collapsed
                self._render_group_header(project, items, collapsed, first=(gi == 0))
                if collapsed:
                    continue
                for s in items:
                    if shown >= limit:
                        overflow += 1
                        continue
                    self._render_row(s)
                    shown += 1
            # 只统计被条数上限挡掉的，主动折叠的不算「另有」
            if overflow:
                tk.Label(self._list, text=f"… 另有 {overflow} 个会话（可折叠项目）",
                         bg=CARD_BG, fg=TXT_FAINT,
                         font=("Microsoft YaHei UI", 8)).pack(anchor="w", pady=(4, 0))

        self.root.update_idletasks()
        self.body.place_configure(width=int(self.cfg["width"]) - 2 * self.radius)
        self.root.update_idletasks()
        h = self.body.winfo_reqheight() + 2 * self.radius
        self.root.geometry(f"{int(self.cfg['width'])}x{h}")
        self._redraw_bg()

    def _render_group_header(self, project: str, items: list[Session],
                             collapsed: bool, first: bool) -> None:
        """项目分组头：可点击折叠。折叠后仍显示会话数与最紧急的状态色。"""
        tk = self.tk
        color = AMBER if any(s.state == "waiting" for s in items) else GREEN
        hdr = tk.Frame(self._list, bg=CARD_BG, cursor="hand2")
        hdr.pack(fill="x", pady=(0 if first else 8, 2))

        chevron = tk.Label(hdr, text="▸" if collapsed else "▾", bg=CARD_BG, fg=TXT_DIM,
                           font=("Segoe UI", 8), width=2)
        chevron.pack(side="left")

        dot = tk.Canvas(hdr, width=7, height=7, bg=CARD_BG, highlightthickness=0)
        dot.pack(side="left", padx=(0, 5), pady=(1, 0))
        dot.create_oval(0, 0, 6, 6, fill=color, outline="")

        short = project if len(project) <= 44 else project[:43] + "…"
        name = tk.Label(hdr, text=short, bg=CARD_BG, fg=TXT, anchor="w",
                        font=("Microsoft YaHei UI", 9, "bold"))
        name.pack(side="left")

        tk.Label(hdr, text=f" {len(items)} ", bg="#2b3038", fg=TXT_DIM,
                 font=("Consolas", 8)).pack(side="left", padx=(6, 0))

        # 折叠时把该项目正在做什么也带一句，避免完全看不到信息
        if collapsed:
            summary = next((s.action_label for s in items if s.action_label), "")
            if summary:
                tk.Label(hdr, text=summary, bg=CARD_BG, fg=TXT_FAINT,
                         font=("Microsoft YaHei UI", 8)).pack(side="left", padx=(8, 0))

        # 整行（含计数徽标、摘要）都可点击折叠
        for w in [hdr] + list(hdr.winfo_children()):
            w.bind("<Button-1>", lambda e, p=project: self._toggle_group(p))
        hdr.bind("<Enter>", lambda e: name.configure(fg=BLUE))
        hdr.bind("<Leave>", lambda e: name.configure(fg=TXT))

    def _toggle_group(self, project: str) -> str:
        if project in self.collapsed:
            self.collapsed.discard(project)
        else:
            self.collapsed.add(project)
        self._render()
        self._save_geometry()
        return "break"          # 不要触发窗口拖动

    def _render_row(self, s: Session) -> None:
        """一条会话：会话名称放在主行，动作在下面。"""
        tk = self.tk
        wrap = int(self.cfg["width"]) - 74
        color = AMBER if s.state == "waiting" else GREEN
        row = tk.Frame(self._list, bg=CARD_BG)
        row.pack(fill="x", pady=(3, 0), padx=(14, 0))

        head = tk.Frame(row, bg=CARD_BG)
        head.pack(fill="x")

        dot = tk.Canvas(head, width=8, height=8, bg=CARD_BG, highlightthickness=0)
        dot.pack(side="left", pady=(4, 0))
        dot.create_oval(1, 1, 7, 7, fill=color, outline="")

        tk.Label(head, text=fmt_elapsed(s.elapsed_sec), bg=CARD_BG, fg=TXT_FAINT,
                 font=("Consolas", 8)).pack(side="right")

        badges = []
        if s.is_subagent:
            badges.append(("子代理", "#2b3038", BLUE))
        if s.state == "waiting":
            badges.append(("等待确认", "#3a3020", AMBER))
        for text, bg, fg in reversed(badges):
            tk.Label(head, text=text, bg=bg, fg=fg,
                     font=("Microsoft YaHei UI", 7), padx=4).pack(side="right", padx=(0, 5))

        # 会话名称：主信息，用亮色
        tk.Label(head, text=s.title, bg=CARD_BG, fg=TXT, anchor="w", justify="left",
                 wraplength=wrap - 60,
                 font=("Microsoft YaHei UI", 9)).pack(side="left", padx=(6, 4))

        if s.action_label:
            act = tk.Frame(row, bg=CARD_BG)
            act.pack(fill="x", padx=(14, 0), pady=(1, 0))
            spin = tk.Label(act, text=SPINNER[self.spin], bg=CARD_BG, fg=color,
                            font=("Segoe UI", 9))
            spin.pack(side="left")
            self._spin_labels.append(spin)
            txt = s.action_label + (f": {s.action_detail}" if s.action_detail else "")
            tk.Label(act, text=txt, bg=CARD_BG, fg=color, anchor="w", justify="left",
                     wraplength=wrap - 16, font=("Microsoft YaHei UI", 8)).pack(
                side="left", padx=(4, 0), fill="x")

    # -- 生命周期 -----------------------------------------------------------
    def quit(self) -> None:
        self._alive = False
        self._save_geometry()
        try:
            self.root.destroy()
        except Exception:
            pass

    def run(self) -> None:
        self._render()
        self.root.mainloop()


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def demo_sessions() -> list[Session]:
    """用于在没有真实运行会话时校验窗口渲染效果（--demo）。

    刻意做成「一个项目下两个会话 + 另外两个项目各一个」，用来检查分组与折叠。
    这里全部使用编造的示例名称——它会出现在 README 的截图里，不要填真实项目名。
    """
    t = int(time.time() * 1000)
    return [
        Session(
            composer_id="demo-1",
            project="acme-web · feat-auth-refresh",
            title="修复登录态刷新竞态", state="running",
            action_label="执行命令",
            action_detail="npm run test:e2e -- --grep \"token refresh\"",
            elapsed_sec=83, last_active_ms=t - 1000, signals=["tool=running"],
        ),
        Session(
            composer_id="demo-2",
            project="acme-web · feat-auth-refresh",
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


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Cursor 运行中会话悬浮窗")
    ap.add_argument("--once", action="store_true", help="打印一次快照后退出")
    ap.add_argument("--watch", action="store_true", help="终端持续输出")
    ap.add_argument("--json", action="store_true", help="以 JSON 输出")
    ap.add_argument("--signals", action="store_true", help="显示判定信号明细")
    ap.add_argument("--demo", action="store_true", help="用假数据渲染窗口（调试 UI）")
    args = ap.parse_args(argv)

    cfg = load_config()
    reader = CursorReader()

    if args.demo:
        win = FloatWindow(cfg, reader, poll=False)
        win.sessions = demo_sessions()
        win.run()
        return 0

    if not reader.available():
        print(f"[cursor-float] 未找到 {DB_PATH}", file=sys.stderr)
        return 2

    if args.once or args.watch or args.json:
        try:
            while True:
                try:
                    snaps = reader.snapshot(cfg)
                    err = ""
                except Exception as exc:                   # noqa: BLE001
                    snaps, err = [], f"{type(exc).__name__}: {exc}"
                if args.json:
                    print(json.dumps({"sessions": [session_to_dict(s) for s in snaps],
                                      "probe": reader.last_probe,
                                      "error": err}, ensure_ascii=False))
                else:
                    out = render_text(snaps, err,
                                      reader.last_probe if args.signals else None)
                    print(out, flush=True)
                if not args.watch:
                    break
                time.sleep(max(0.2, float(cfg["interval_ms"]) / 1000.0))
        except KeyboardInterrupt:
            pass
        return 0

    FloatWindow(cfg, reader).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
