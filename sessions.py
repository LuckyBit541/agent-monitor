#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
cursor-float 的数据层：只读读取 Cursor 的 state.vscdb，判定哪些会话正在运行。

数据来源（全部只读，不写入 Cursor 任何文件）：
  %APPDATA%\Cursor\User\globalStorage\state.vscdb
    - composerHeaders                    每个会话一行（标题/工作区/最后活动/是否归档/子代理）
    - cursorDiskKV['composerData:<id>']   会话内容，用于「有没有在变」的判定与当前动作
    - cursorDiskKV['bubbleId:<id>:<bid>'] 最近一条气泡，toolFormerData 给出「当前在跑什么工具」

判定依据见 CursorReader._classify 的注释：落盘的 status / generatingBubbleIds 在生成期间
并不刷新，唯一可靠的是 composerData 的内容在变化，以及工具 status == "loading"。

也可以直接当命令行工具用：
    python sessions.py --once              # 打印一次快照
    python sessions.py --watch --signals   # 滚动输出，并解释每个会话的判定依据
    python sessions.py --json              # JSON 输出
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

# --------------------------------------------------------------------------
# 默认配置（可被脚本同目录的 config.json 覆盖）
# --------------------------------------------------------------------------

DEFAULTS: dict[str, Any] = {
    "interval_ms": 900,        # 轮询间隔
    "grace_seconds": 25,       # 头部时间戳在此秒数内仍视为运行中，用于回合刚开始的时刻
    "header_scan": 60,         # 读取最近多少条 composerHeaders
    "live_scan": 8,            # 无论时间戳如何，都强制检查的最靠前会话数（兜底）
    "first_scan": 12,          # 启动后第一轮最多检查多少个会话（避免首屏卡顿）
    "candidate_window_sec": 900,  # 头部时间戳在这个窗口内的会话才去解析状态对象
    "track_keep_sec": 600,     # 曾经活跃过的会话，继续检查多久
    "activity_grace_sec": 45,  # 观察到内容变化后，还视为「运行中」多久
    "max_rows": 8,             # 最多显示几条会话
}

SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = SCRIPT_DIR / "config.json"

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
# 命令行（排查用：解释每个会话为什么显示 / 为什么不显示）
# --------------------------------------------------------------------------

def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Cursor 运行中会话的数据层 / 诊断工具")
    ap.add_argument("--once", action="store_true", help="打印一次快照后退出")
    ap.add_argument("--watch", action="store_true", help="终端持续输出")
    ap.add_argument("--json", action="store_true", help="以 JSON 输出")
    ap.add_argument("--signals", action="store_true", help="显示每个会话的判定依据明细")
    ap.add_argument("--settle", type=float, default=4.0,
                    help="--once 最多观察这么多秒再下结论：判定依赖内容变化，"
                         "只查一轮可能误报为空（0 表示只查一轮）")
    args = ap.parse_args(argv)

    cfg = load_config()
    reader = CursorReader()
    if not reader.available():
        print(f"[cursor-float] 未找到 {DB_PATH}", file=sys.stderr)
        return 2

    if not (args.once or args.watch or args.json):
        ap.print_help()
        return 0

    interval = max(0.2, float(cfg["interval_ms"]) / 1000.0)
    settle = 0.0 if args.watch else max(0.0, float(args.settle))
    deadline = time.time() + settle
    rounds = 0
    try:
        # 判定依赖「内容较上一轮有变化」，所以只看一轮可能误报为空。
        while True:
            rounds += 1
            try:
                snaps, err = reader.snapshot(cfg), ""
            except Exception as exc:                       # noqa: BLE001
                snaps, err = [], f"{type(exc).__name__}: {exc}"
            if args.watch or snaps or time.time() >= deadline:
                break
            time.sleep(interval)
        _emit(args, snaps, err, reader, rounds, settle)

        while args.watch:
            time.sleep(interval)
            try:
                snaps, err = reader.snapshot(cfg), ""
            except Exception as exc:                       # noqa: BLE001
                snaps, err = [], f"{type(exc).__name__}: {exc}"
            _emit(args, snaps, err, reader, 0, 0.0)
    except KeyboardInterrupt:
        pass
    return 0


def _emit(args, snaps, err, reader, rounds: int, settle: float) -> None:
    if args.json:
        print(json.dumps({"sessions": [session_to_dict(s) for s in snaps],
                          "probe": reader.last_probe,
                          "rounds": rounds,
                          "error": err}, ensure_ascii=False))
        return
    out = render_text(snaps, err, reader.last_probe if args.signals else None)
    if rounds > 1:
        out += (f"\n  （观察了 {rounds} 轮 / {settle:g} 秒；"
                f"判定依赖内容变化，单轮可能看不到正在跑的会话）")
    print(out, flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
