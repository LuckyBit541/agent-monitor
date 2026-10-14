#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""cursor-float 的窗口层：一个无边框、逐像素半透明的浮窗，显示正在运行的 Cursor 会话。

窗口是 Qt 的 WA_TranslucentBackground 窗口，页面里每个像素的 alpha 都直接和桌面
混合，所以「透明度」只淡化卡片背景（CSS 变量 --bg-alpha），文字和圆点始终不透明。
圆角之外的区域是真正透明的。

界面逻辑在 card.html，数据判定在 sessions.py。三种显示模式（详细 / 简约 / 最简）
由标题栏最左边的按钮循环切换。
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from sessions import CursorReader, load_config
from window_state import (
    HANDLE_H,
    HANDLE_W,
    MAX_HEIGHT,
    MIN_HEIGHT,
    MODES,
    STATE_PATH,
    WIDTHS,
    dock_side,
    demo_sessions,
    edge_at,
    expanded_geometry,
    folded_geometry,
    group_sessions,
    hot_zone,
    initial_position,
    load_state,
    normalize_mode,
    normalize_opacity,
    payload as build_payload,
    save_state,
    saved_corner,
    signature,
)

CARD_PATH = Path(__file__).with_name("card.html")

# 这台机器上 QtWebEngine 的 GPU 合成会失败（日志里是 "Compositor returned null
# texture"），表现是窗口完全透明、屏幕上什么都看不到——很容易被误判成程序没启动。
# 必须在 QApplication 创建之前关掉 GPU 合成。想自己调的话，启动前设这个环境变量即可。
os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS",
                      "--disable-gpu --disable-gpu-compositing")


def _fail(message: str) -> None:
    """报告致命的启动问题。

    启动器用 pythonw 运行本程序，那里没有控制台能显示错误，所以同时落到状态文件旁边。
    """
    print(message, file=sys.stderr)
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        (STATE_PATH.parent / "last-error.log").write_text(
            f"{datetime.now().isoformat(timespec='seconds')} {message}\n",
            encoding="utf-8",
        )
    except OSError:
        pass


try:
    from PySide6.QtCore import QFile, QIODevice, QObject, QPoint, QRect, Qt, QTimer, QUrl, Signal, Slot
    from PySide6.QtGui import QGuiApplication
    from PySide6.QtWebChannel import QWebChannel
    from PySide6.QtWebEngineCore import QWebEngineScript
    from PySide6.QtWebEngineWidgets import QWebEngineView
    from PySide6.QtWidgets import QApplication
except ImportError:                                   # pragma: no cover
    _fail("缺少依赖 PySide6，请先运行: pip install -r requirements.txt")
    sys.exit(1)


WINDOW_TITLE = "Cursor 运行中会话"
DEBUG = bool(os.environ.get("CURSOR_FLOAT_DEBUG"))

# 贴边自动收起：鼠标离开多久后收起、多久轮询一次鼠标位置、
# 以及收起后判定「鼠标靠近」的额外容差。
FOLD_DELAY_MS = 700
MOUSE_POLL_MS = 120
HOT_PAD = 10

# 首帧的估算高度，页面测出真实值后会立刻纠正
WINDOW_HEIGHT = 200

# 页面可以调用的方法。不在这个表里的一律拒绝。
PAGE_CALLS = (
    "refresh",
    "request_refresh",
    "report_size",
    "set_mode",
    "set_opacity",
    "set_pinned",
    "set_dock",
    "expand_now",
    "toggle_group",
    "expand_all",
    "collapse_all",
    "start_move",
    "hide",
)
# 会读数据库、耗时较久的调用放到后台线程，避免界面卡住
BACKGROUND_CALLS = frozenset({"refresh"})

LOG = logging.getLogger("cursor-float")



# -------------------------------------------------------------------- 桥接

class Bridge(QObject):
    """卡片页面的 Python 端，通过 QWebChannel 访问。

    页面调用 call(id, name, args)，答案回到 reply；后台轮询的新快照走 pushed。
    两者都传 JSON，避免任何 Qt 类型转换。
    """

    reply = Signal(int, str)
    pushed = Signal(str)

    def __init__(self, state: dict, cfg: dict, demo: bool = False) -> None:
        super().__init__()
        self._state = state
        self._cfg = cfg
        self._demo = demo
        self._view: CardWindow | None = None
        self._reader = CursorReader()
        # 轮询线程和 refresh 调用都会用 reader，而 reader 内部的指纹是有状态的，
        # 必须串行化，否则判定会互相干扰。
        self._lock = threading.Lock()
        self._refresh_event = threading.Event()
        self._closed = threading.Event()
        self._last_signature: tuple | None = None
        self._last_error = ""
        self._worker: threading.Thread | None = None

    # ------------------------------------------------------------ 分发

    @Slot(int, str, str)
    def call(self, request_id: int, name: str, args_json: str) -> None:
        if name not in PAGE_CALLS:
            self._answer(request_id, error=f"unknown call: {name}")
            return
        try:
            args = json.loads(args_json) if args_json else []
        except json.JSONDecodeError:
            args = []
        if not isinstance(args, list):
            args = [args]
        if name in BACKGROUND_CALLS:
            threading.Thread(target=self._run, args=(request_id, name, args),
                             daemon=True).start()
        else:
            self._run(request_id, name, args)

    def _run(self, request_id: int, name: str, args: list) -> None:
        try:
            value = getattr(self, name)(*args)
        except Exception as exc:                       # noqa: BLE001 - 把原因回给页面
            LOG.warning("调用 %s 失败: %s", name, exc)
            self._answer(request_id, error=str(exc))
            return
        self._answer(request_id, value)

    def _answer(self, request_id: int, value: Any = None, error: str | None = None) -> None:
        payload = {"ok": error is None, "value": value, "error": error}
        self.reply.emit(request_id, json.dumps(payload, ensure_ascii=False))

    # ------------------------------------------------------------ 设置

    def _settings(self) -> dict:
        return {
            "mode": normalize_mode(self._state.get("mode")),
            "opacity": normalize_opacity(self._state.get("opacity", 1)),
            "pinned": bool(self._state.get("pinned", True)),
            "dock": bool(self._state.get("dock", False)),
            "folded": bool(self._view is not None and self._view.folded),
            "side": (self._view.side if self._view is not None else "right"),
        }

    def _store(self, **changes: Any) -> dict:
        self._state.update(changes)
        save_state(self._state)
        return self._settings()

    def _collapsed(self) -> set:
        return set(self._state.get("collapsed") or [])

    # ------------------------------------------------------- 页面可调用

    def _collect(self) -> dict:
        """读一次会话状态，整理成页面需要的结构。"""
        error = ""
        if self._demo:
            sessions = demo_sessions()
        else:
            try:
                with self._lock:
                    sessions = self._reader.snapshot(self._cfg)
            except Exception as exc:                   # noqa: BLE001
                sessions, error = [], f"{type(exc).__name__}: {exc}"
        collapsed = self._collapsed()
        self._last_signature = signature(sessions, collapsed, error)
        self._last_error = error
        return build_payload(
            sessions, collapsed, self._settings(),
            float(self._cfg["interval_ms"]) / 1000.0,
            datetime.now().strftime("%H:%M:%S"), error,
        )

    def refresh(self) -> dict:
        return self._collect()

    def request_refresh(self) -> bool:
        self._refresh_event.set()
        return True

    def report_size(self, width: float, height: float) -> bool:
        """按卡片实际尺寸调整窗口，避免背景露出或内容被裁切。"""
        if self._view is None:
            return False
        try:
            w = int(round(float(width)))
            h = int(round(float(height)))
        except (TypeError, ValueError):
            return False
        if h <= 0 or w <= 0:
            return False
        self._view.fit_size(w, h)
        return True

    def set_mode(self, mode: str) -> dict:
        return self._store(mode=normalize_mode(mode))

    def set_opacity(self, opacity: float) -> dict:
        return self._store(opacity=normalize_opacity(opacity))

    def set_pinned(self, pinned: bool) -> dict:
        settings = self._store(pinned=bool(pinned))
        if self._view is not None:
            self._view.set_on_top(settings["pinned"])
        return settings

    def set_dock(self, dock: bool) -> dict:
        """开关「贴边自动收起」。打开时立即吸附到最近的那条竖边。"""
        settings = self._store(dock=bool(dock))
        if self._view is not None:
            self._view.apply_dock(settings["dock"])
        self.pushed.emit(json.dumps(self._collect(), ensure_ascii=False))
        return settings

    def notify_fold(self) -> None:
        """收起/展开后立刻让页面切到对应的外观，不等下一次轮询。"""
        try:
            self.pushed.emit(json.dumps(self._collect(), ensure_ascii=False))
        except Exception as exc:                       # noqa: BLE001
            LOG.warning("推送收起状态失败: %s", exc)

    def expand_now(self) -> bool:
        """点击收起后的细杆：立刻展开，并把「靠近才展开」重新武装起来。"""
        if self._view is None:
            return False
        self._view.unfold()
        self._view._armed = False
        self._view._left_at = None
        return True

    def toggle_group(self, project: str) -> dict:
        collapsed = self._collapsed()
        name = str(project)
        if name in collapsed:
            collapsed.discard(name)
        else:
            collapsed.add(name)
        self._store(collapsed=sorted(collapsed))
        # 折叠状态变了要立刻重画；轮询线程也会带上新状态
        self.pushed.emit(json.dumps(self._collect(), ensure_ascii=False))
        return self._settings()

    def expand_all(self) -> dict:
        self._store(collapsed=[])
        self.pushed.emit(json.dumps(self._collect(), ensure_ascii=False))
        return self._settings()

    def collapse_all(self) -> dict:
        projects = {g["project"] for g in group_sessions(self._reader_sessions(), self._collapsed())}
        self._store(collapsed=sorted(projects))
        self.pushed.emit(json.dumps(self._collect(), ensure_ascii=False))
        return self._settings()

    def _reader_sessions(self) -> list:
        if self._demo:
            return demo_sessions()
        try:
            with self._lock:
                return self._reader.snapshot(self._cfg)
        except Exception:                              # noqa: BLE001
            return []

    def start_move(self) -> bool:
        if self._view is None:
            return False
        handle = self._view.windowHandle()
        return bool(handle and handle.startSystemMove())

    def hide(self) -> bool:
        if self._view is not None:
            self._view.close()
        return True

    # ------------------------------------------------------------ 生命周期

    def start_worker(self) -> None:
        thread = self._worker
        if thread is not None and thread.is_alive():
            return
        self._worker = threading.Thread(target=_serve, args=(self,), daemon=True)
        self._worker.start()

    def _shutdown(self) -> None:
        self._closed.set()
        self._refresh_event.set()


def _serve(bridge: Bridge) -> None:
    """后台循环：按轮询间隔检查会话，只有内容真的变了才推给页面。

    只在变化时推送很重要：每次推送都会触发页面重排和高度自测，无脑推送会让
    窗口不停地重新测量。
    """
    interval = max(0.2, float(bridge._cfg["interval_ms"]) / 1000.0)
    while not bridge._closed.is_set():
        bridge._refresh_event.wait(interval)
        bridge._refresh_event.clear()
        if bridge._closed.is_set():
            return
        try:
            payload = bridge._collect()
        except Exception as exc:                       # noqa: BLE001 - 别让循环挂掉
            LOG.warning("采集会话状态失败: %s", exc)
            continue
        if bridge._closed.is_set():
            return
        if payload["error"] or payload["running"] or bridge._last_signature is not None:
            bridge.pushed.emit(json.dumps(payload, ensure_ascii=False))


# ------------------------------------------------------------------- 窗口

def _bridge_script() -> QWebEngineScript:
    """桥接的页面端：window.cursorBridge，方法都返回 Promise。"""
    source = QFile(":/qtwebchannel/qwebchannel.js")
    if not source.open(QIODevice.ReadOnly):
        raise RuntimeError("Qt 资源里缺少 qwebchannel.js")
    library = bytes(source.readAll()).decode("utf-8")
    source.close()
    shim = """
new QWebChannel(qt.webChannelTransport, (channel) => {
  const host = channel.objects.bridge;
  const pending = new Map();
  let next = 0;
  host.reply.connect((id, payload) => {
    const waiter = pending.get(id);
    if (!waiter) return;
    pending.delete(id);
    const answer = JSON.parse(payload);
    if (answer.ok) waiter.resolve(answer.value);
    else waiter.reject(new Error(answer.error));
  });
  host.pushed.connect((payload) => {
    if (window.cursorFloat) window.cursorFloat.apply(JSON.parse(payload));
  });
  const bridge = {};
  for (const name of %s) {
    bridge[name] = (...args) => new Promise((resolve, reject) => {
      const id = ++next;
      pending.set(id, { resolve, reject });
      host.call(id, name, JSON.stringify(args));
    });
  }
  window.cursorBridge = bridge;
  window.dispatchEvent(new Event("cursorbridgeready"));
});
""" % json.dumps(PAGE_CALLS)
    script = QWebEngineScript()
    script.setName("cursor-bridge")
    script.setSourceCode(library + "\n" + shim)
    script.setInjectionPoint(QWebEngineScript.InjectionPoint.DocumentReady)
    script.setWorldId(QWebEngineScript.ScriptWorldId.MainWorld)
    script.setRunsOnSubFrames(False)
    return script


def _screen_area(x: int, y: int) -> tuple[int, int, int, int]:
    """(x, y) 所在显示器的工作区，显示器不在了就退回主屏。"""
    screen = QGuiApplication.screenAt(QPoint(x, y)) or QGuiApplication.primaryScreen()
    rect = screen.availableGeometry()
    return rect.left(), rect.top(), rect.left() + rect.width(), rect.top() + rect.height()


class CardWindow(QWebEngineView):
    """无边框、逐像素半透明的窗口，承载 card.html。"""

    def __init__(self, bridge: Bridge) -> None:
        super().__init__()
        self._bridge = bridge
        self._ready = False
        self._width = WIDTHS[normalize_mode(bridge._state.get("mode"))]
        self.folded = False                 # 是否已收起成细条
        self.side = "right"                 # 吸附在哪条竖边
        self._expanded: tuple[int, int, int, int] | None = None
        self._left_at: float | None = None
        self._armed = False                 # 收起后需先离开热区，才允许再次展开
        state = bridge._state

        self.setWindowTitle(WINDOW_TITLE)
        flags = Qt.WindowType.FramelessWindowHint | Qt.WindowType.NoDropShadowWindowHint
        if bool(state.get("pinned", True)):
            flags |= Qt.WindowType.WindowStaysOnTopHint
        self.setWindowFlags(flags)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
        self.page().setBackgroundColor(Qt.GlobalColor.transparent)

        self._channel = QWebChannel(self.page())
        self._channel.registerObject("bridge", bridge)
        self.page().setWebChannel(self._channel)
        self.page().scripts().insert(_bridge_script())

        # 拖动过程中会持续触发 moveEvent，等停下来再保存位置
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(400)
        self._save_timer.timeout.connect(self._on_move_settled)

        x, y = initial_position(state, _screen_area(*saved_corner(state)), self._width)
        if DEBUG:
            LOG.info("初始位置=%s,%s 模式=%s 贴边=%s", x, y, state.get("mode"),
                     state.get("dock"))
        self.setGeometry(x, y, self._width, WINDOW_HEIGHT)
        self.load(QUrl.fromLocalFile(str(CARD_PATH)))
        self._ready = True

        # 用轮询鼠标位置判断进出，而不是靠 enter/leave 事件：卡片是半透明窗口，
        # 圆角之外那些像素不接收事件，事件方式会漏判。
        self._mouse_timer = QTimer(self)
        self._mouse_timer.setInterval(MOUSE_POLL_MS)
        self._mouse_timer.timeout.connect(self._track_mouse)
        self._mouse_timer.start()

    # ---------------------------------------------------------- 贴边自动收起

    def _area(self) -> tuple[int, int, int, int]:
        return _screen_area(self.x(), self.y())

    def apply_dock(self, dock: bool) -> None:
        """开关贴边自动收起。

        打开**不会**挪动窗口——贴边是位置驱动的：只有窗口真的被拖到边上时才收起，
        放在屏幕中间就是普通浮窗，和以前一样。
        """
        if not dock:
            if self.folded:
                self.unfold()
            return
        side = self._at_edge()
        if side:
            self.side = side
            self._snap_to_edge()
        self._left_at = None

    def _at_edge(self) -> str | None:
        """窗口离哪条竖边足够近，够近才返回它。"""
        return edge_at(self._area(), self.x(), self.width())

    def _snap_to_edge(self) -> None:
        if self.folded:
            return
        area = self._area()
        x = area[0] if self.side == "left" else area[2] - self.width()
        y = max(area[1], min(self.y(), area[3] - self.height() - 4))
        if (x, y) != (self.x(), self.y()):
            if DEBUG:
                LOG.info("吸附: (%s,%s) -> (%s,%s) side=%s", self.x(), self.y(), x, y,
                         self.side)
            self.move(x, y)

    def fold(self) -> None:
        """收起成贴边的细条。只应在窗口已经贴着边时调用。"""
        if self.folded:
            return
        area = self._area()
        self.side = self._at_edge() or dock_side(area, self.x(), self.width())
        self._expanded = (self.x(), self.y(), self.width(), self.height())
        hx, hy = folded_geometry(area, *self._expanded)
        self.folded = True
        self.setGeometry(hx, hy, HANDLE_W, HANDLE_H)
        self._left_at = None
        self._armed = False
        self._bridge.notify_fold()
        if DEBUG:
            zone = hot_zone((hx, hy, HANDLE_W, HANDLE_H), self._expanded, HOT_PAD)
            LOG.info("收起: 原=%s -> 细条=(%s,%s) 热区=%s", self._expanded, hx, hy, zone)

    def hold_folded(self) -> None:
        """收起并停掉鼠标轮询——仅供截图使用。

        否则鼠标只要经过边缘就会把细条撬开，截出来的就不是收起状态了。
        """
        self.fold()
        self._mouse_timer.stop()

    def unfold(self) -> None:
        """从细条展开回卡片。"""
        if not self.folded:
            return
        source = self._expanded or (self.x(), self.y(), self._width, 260)
        ex, ey = expanded_geometry(self._area(), self.x(), self.y(), source[2], source[3])
        self.folded = False
        self._width = source[2]
        self.setGeometry(ex, ey, source[2], source[3])
        self._bridge.notify_fold()
        if DEBUG:
            LOG.info("展开: 细条=(%s,%s) -> (%s,%s) %sx%s", self.x(), self.y(), ex, ey,
                     source[2], source[3])

    def _track_mouse(self) -> None:
        if not self._ready or not self._bridge._state.get("dock"):
            return
        from PySide6.QtGui import QCursor

        pos = QCursor.pos()
        rect = self.frameGeometry()
        if self.folded:
            # 热区只沿边缘方向放宽，并且不超出展开后那张卡片的范围。
            # 一旦热区伸到卡片之外，指针落进那条缝就会「展开→收起→展开」
            # 无限抖动——矮卡片（最简模式）特别容易撞上。
            hot = None
            if self._expanded:
                hx, hy = folded_geometry(self._area(), *self._expanded)
                hot = QRect(*hot_zone((hx, hy, HANDLE_W, HANDLE_H), self._expanded, HOT_PAD))
            if hot is None or hot.isEmpty() or not hot.contains(pos):
                self._armed = True          # 鼠标离开过，下次靠近才展开
            elif self._armed:
                self._armed = False
                if DEBUG:
                    LOG.info("悬停展开: 鼠标=(%s,%s) 热区=%s", pos.x(), pos.y(), hot)
                self.unfold()
            return

        # 不在边上就完全是普通浮窗：不收起，也不吸附
        side = self._at_edge()
        if side is None:
            self._left_at = None
            return
        self.side = side

        if rect.contains(pos):
            self._left_at = None
            return
        now = time.monotonic()
        if self._left_at is None:
            self._left_at = now
        elif (now - self._left_at) * 1000 >= FOLD_DELAY_MS:
            self.fold()

    def fit_size(self, width: int, height: int) -> None:
        """按卡片上报的尺寸调整窗口，并保证窗口整体留在屏幕内。"""
        if self.folded:
            return                    # 收起时尺寸由 fold 决定，页面不要插话
        self._width = max(120, min(760, width))
        _left, top, right, bottom = self._area()
        room = bottom - self.y() - 8
        target = max(MIN_HEIGHT, min(MAX_HEIGHT, height, max(MIN_HEIGHT, room)))
        if target != self.height() or self._width != self.width():
            if DEBUG:
                LOG.info("适配: 上报=%sx%s -> %sx%s", width, height, self._width, target)
            self.resize(self._width, target)
        # 高度变了可能把底边推出屏幕，重新夹一次
        x = max(_left, min(self.x(), right - self._width))
        y = max(top, min(self.y(), bottom - target - 4))
        if (x, y) != (self.x(), self.y()):
            self.move(x, y)
        # 内容变宽变窄时，只有本来就在边上才继续贴着，别把窗口拽过去
        if self._bridge._state.get("dock") and self._at_edge():
            self._snap_to_edge()

    def set_on_top(self, pinned: bool) -> None:
        # QWindow.setFlag 原地更新原生窗口；QWidget 的等价操作会重建窗口，
        # 那会闪一下并重新加载页面。
        handle = self.windowHandle()
        if handle is not None:
            handle.setFlag(Qt.WindowType.WindowStaysOnTopHint, pinned)

    def moveEvent(self, event) -> None:                # noqa: N802 - Qt 覆写
        super().moveEvent(event)
        if self._ready:
            self._save_timer.start()

    def _on_move_settled(self) -> None:
        """拖动停下后：只有拖到了边上才吸附并进入贴边收起，否则保持自由摆放。"""
        if self._bridge._state.get("dock") and not self.folded:
            side = self._at_edge()
            if side:
                self.side = side
                self._snap_to_edge()
        self._save_position()

    def closeEvent(self, event) -> None:               # noqa: N802 - Qt 覆写
        self._save_timer.stop()
        self._mouse_timer.stop()
        self._save_position()
        self._bridge._shutdown()
        super().closeEvent(event)

    def _save_position(self) -> None:
        # 收起时记录展开状态的位置，下次启动才不会以细条的位置为准
        if self.folded and self._expanded:
            x, y = self._expanded[0], self._expanded[1]
        else:
            x, y = self.x(), self.y()
        self._bridge._store(x=x, y=y)


def _capture(path: str, view: CardWindow) -> None:
    """--shot：把窗口当前的样子截成 PNG。"""
    from PIL import ImageGrab

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    ratio = view.devicePixelRatioF()
    rect = view.frameGeometry()
    ImageGrab.grab(bbox=(
        int(rect.left() * ratio), int(rect.top() * ratio),
        int((rect.left() + rect.width()) * ratio),
        int((rect.top() + rect.height()) * ratio),
    )).save(target)
    view.close()


SELFTEST_JS = """
(() => {
  const out = {steps: []};
  out.hasBridge = Boolean(window.cursorBridge);
  const body = () => document.body.className;
  out.startMode = body();
  document.getElementById('btnMode').click();
  out.afterModeClick = body();
  document.getElementById('btnAlpha').click();
  out.alpha = getComputedStyle(document.documentElement).getPropertyValue('--bg-alpha').trim();
  document.getElementById('btnPin').click();
  out.pinActive = document.getElementById('btnPin').classList.contains('on');
  out.hasDockButton = Boolean(document.getElementById('btnDock'));
  out.hasHandle = Boolean(document.getElementById('handle'));
  const head = document.querySelector('.ghead');
  out.hasGroups = Boolean(head);
  if (head) { head.click(); out.afterGroupClick = document.querySelector('.ghead').className; }
  out.title = document.getElementById('title').textContent;
  return JSON.stringify(out);
})();
"""


def _selftest(view: CardWindow) -> None:
    """点一遍卡片上的按钮，回读页面状态，证明交互链路是通的。"""
    def report(result: Any) -> None:
        print("[selftest]", result)
        view.close()
        QApplication.quit()

    view.page().runJavaScript(SELFTEST_JS, report)



def main() -> None:
    parser = argparse.ArgumentParser(description="悬浮显示 Cursor 中正在运行的会话")
    parser.add_argument("--shot", help=argparse.SUPPRESS)
    parser.add_argument("--selftest", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--fold", action="store_true",
                        help=argparse.SUPPRESS)          # 截图用：直接以收起状态启动
    parser.add_argument("--mode", choices=MODES, help="以指定模式启动（会被写入状态）")
    parser.add_argument("--demo", action="store_true",
                        help="用编造的会话数据渲染（截图 / 看外观用）")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="[cursor-float] %(message)s")

    application = QApplication(sys.argv[:1])
    state = load_state()
    if args.mode:
        state["mode"] = args.mode
    cfg = load_config()
    if DEBUG:
        LOG.info("状态文件=%s 状态=%s 配置=%s", STATE_PATH, state, cfg)
    bridge = Bridge(state, cfg, demo=args.demo)
    view = CardWindow(bridge)
    bridge._view = view
    bridge.start_worker()
    view.show()
    if state.get("dock"):
        view.apply_dock(True)
    if args.fold:
        # 截图用：不等鼠标走开，直接收起并锁住
        QTimer.singleShot(1200, view.hold_folded)
    if args.shot:
        QTimer.singleShot(6000, lambda: _capture(args.shot, view))
    if args.selftest:
        QTimer.singleShot(5000, lambda: _selftest(view))
    application.exec()
    bridge._shutdown()


if __name__ == "__main__":
    main()
    sys.exit(0)
