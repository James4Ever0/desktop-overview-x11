#!/usr/bin/env python3
"""
demo-keyevent-listener-with-idle-chunking-and-focus-change-chunking.py

在 demo-keyevent-listener.py 的基础上增加【分段 / chunking】：
  1) 采集可见按键 (chars / space / tab / enter / backspace)；
  2) 把连续输入聚合成一个【短字符串 segment】（当前累积段）；
  3) 把完成的 segment 连同【该段第一个键的时间戳 start_ts】一起推入 buffer
     （一个 list[(start_ts, text)]）。

切段 (cut / flush) 的三个条件：
  - 空闲 IDLE_TIMEOUT_S 秒没有任何可见按键；或
  - 发生 X11 窗口焦点切换事件 (_NET_ACTIVE_WINDOW 变化)；或
  - 当前聚焦窗口的标题发生变化 (_NET_WM_NAME / WM_NAME 变化)——
    即使焦点没变，标题变了也切段（参考 demo-get-window-title-change.py）。

也就是说：在同一个窗口、同一个标题下连续打字会聚成一段；一旦停手几秒、
切到别的窗口、或当前窗口标题变了，当前这段就被定型并存进 buffer。

线程模型（都是 daemon 线程，主线程只负责存活 + 退出时收尾）：
  - pynput Listener 线程   —— 捕获按键，追加到当前 segment
  - idle-watcher 线程       —— 周期检查空闲，超时则 flush 当前 segment
  - FocusTitleMonitor 线程  —— python-xlib 监听 root 的 _NET_ACTIVE_WINDOW
                               (焦点) 与聚焦窗口的 _NET_WM_NAME/WM_NAME (标题)，
                               焦点切换或标题变化时 flush 当前 segment
  - buffer-dumper 线程      —— 仅用于演示：周期打印整个 buffer

依赖：
    pip install pynput
    sudo apt install python3-xlib   (或 pip install python-xlib)   # 焦点切段需要
没有 X11 / python-xlib 时仍可运行，只是退化为【只按空闲切段】。
"""

import sys
import time
import threading
from datetime import datetime

from pynput.keyboard import Listener, Key, KeyCode

# python-xlib 只在做焦点/标题切段时需要；缺失则退化为只按空闲切段。
try:
    from Xlib import X, Xatom, display, error
    _HAVE_XLIB = True
except ImportError:
    _HAVE_XLIB = False


# ---------------------------- 配置 / Config ----------------------------
IDLE_TIMEOUT_S = 3.0            # 空闲多少秒后切断当前 segment
IDLE_CHECK_INTERVAL_S = 0.5     # idle-watcher 的检查间隔
BUFFER_DUMP_INTERVAL_S = 10     # 演示用：每隔 N 秒打印一次整个 buffer
APPLY_BACKSPACE = True          # True: backspace 删掉当前段最后一个字符；False: 记成 '\b'


# ---------------------------- 全局状态 / Global state ----------------------------
# buffer：完成的分段结果。每个元素是 (start_ts, text)：
#   start_ts —— 该段【第一个键】被按下的时间 (epoch 秒)
#   text     —— 该段聚合后的字符串
# 这是本 demo 的产出。
_buffer = []                    # list[tuple[float, str]]

# 当前正在累积的 segment（用 list[str] 便于 backspace 高效删尾）
_current_chars = []
_segment_start_ts = None        # 当前段第一个键的时间
_last_key_ts = None             # 当前段最近一个键的时间

# 一把锁保护以上所有可变状态（按键线程 / idle 线程 / 焦点线程都会改）
_lock = threading.Lock()


def log(msg):
    """带时间戳、立即 flush 的 stderr 输出。"""
    print(f"{time.strftime('%H:%M:%S')} | {msg}", file=sys.stderr, flush=True)


def _fmt_ts(ts):
    """epoch 秒 -> HH:MM:SS.mmm；None -> '--'。"""
    if ts is None:
        return "--"
    return datetime.fromtimestamp(ts).strftime('%H:%M:%S.%f')[:-3]


def _preview(text, n=60):
    """把段内容裁成单行预览：转义不可见字符，过长则截断并标注。"""
    shown = text if len(text) <= n else text[:n]
    body = repr(shown)
    if len(text) > n:
        body = body + f" …(+{len(text) - n} chars)"
    return body


# ---------------------------- 按键 -> 可见字符 ----------------------------
def _key_to_repr(key):
    """将 pynput key 转换为可打印字符；非可见键返回 None。"""
    if isinstance(key, KeyCode):
        return key.char if key.char is not None else None

    if isinstance(key, Key):
        allowed = {
            Key.space: ' ',
            Key.tab: '\t',
            Key.enter: '\n',
            Key.backspace: '\b',
        }
        return allowed.get(key, None)

    return None


# ---------------------------- 切段 / flush ----------------------------
def _flush_segment(reason):
    """把当前累积的 segment 定型为 (start_ts, text) 并推入 buffer；清空累积状态。

    start_ts 即该段第一个键的按下时间 (_segment_start_ts)。
    reason: 'idle' | 'focus_change' | 'shutdown' —— 仅用于日志。
    无内容时不产生空段。线程安全：自行持锁。
    """
    global _segment_start_ts, _last_key_ts
    with _lock:
        if not _current_chars:
            return
        segment = ''.join(_current_chars)
        start_ts = _segment_start_ts
        end_ts = _last_key_ts
        _buffer.append((start_ts, segment))
        total = len(_buffer)
        total_chars = sum(len(t) for _, t in _buffer)
        duration = (end_ts - start_ts) if (start_ts and end_ts) else 0.0
        log(f"[flush] reason={reason:<12} chunk #{total} "
            f"(total chunks={total}, total chars={total_chars}) | "
            f"len={len(segment)} chars | "
            f"start={_fmt_ts(start_ts)} end={_fmt_ts(end_ts)} dur={duration:.2f}s | "
            f"preview={_preview(segment)}")
        _current_chars.clear()
        _segment_start_ts = None
        _last_key_ts = None


# ---------------------------- 按键回调 ----------------------------
def _on_key_press(key):
    """pynput 回调：把可见按键追加到当前 segment。"""
    global _segment_start_ts, _last_key_ts
    rep = _key_to_repr(key)
    if rep is None:
        return True   # 忽略非可见键，继续监听

    ts = time.time()
    with _lock:
        if rep == '\b' and APPLY_BACKSPACE:
            # backspace：尽力删掉当前段最后一个字符（编辑保真，best-effort）
            if _current_chars:
                _current_chars.pop()
        else:
            if not _current_chars:
                _segment_start_ts = ts
            _current_chars.append(rep)
        _last_key_ts = ts
    return True       # 始终返回 True 以保持监听器运行


# ---------------------------- idle 切段线程 ----------------------------
def _idle_watcher():
    """周期检查：当前段非空且空闲超过 IDLE_TIMEOUT_S，则按 idle 切段。"""
    while True:
        time.sleep(IDLE_CHECK_INTERVAL_S)
        with _lock:
            idle_due = (
                _current_chars
                and _last_key_ts is not None
                and (time.time() - _last_key_ts) >= IDLE_TIMEOUT_S
            )
        if idle_due:
            _flush_segment("idle")


# ---------------------------- 焦点 / 标题切段：X11 监听 ----------------------------
class FocusTitleMonitor:
    """用 python-xlib 监听两类信号（参考 demo-get-window-title-change.py）：

      1) 焦点切换 —— root 的 _NET_ACTIVE_WINDOW 变化；
      2) 标题变化 —— 【当前聚焦窗口】的 _NET_WM_NAME / WM_NAME 变化
                     （焦点没变、但标题变了，也作为切段信号）。

    自带一个独立的 X 连接，在自己的线程里跑 next_event() 阻塞循环。
    只对【当前聚焦窗口】select 标题属性变化——别的窗口后台改标题不关心。
    """

    # 标题相关 atom（任意一个变化都视作标题变化）
    TITLE_ATOMS = {"_NET_WM_NAME", "WM_NAME", "_NET_WM_VISIBLE_NAME"}

    def __init__(self, on_focus_change, on_title_change):
        self.on_focus_change = on_focus_change   # on_focus_change(new_wid, old_wid)
        self.on_title_change = on_title_change   # on_title_change(wid, old_title, new_title)
        self.dpy = display.Display()
        self.root = self.dpy.screen().root

        self.A_NET_ACTIVE_WINDOW = self.dpy.intern_atom("_NET_ACTIVE_WINDOW")
        self.A_NET_WM_NAME = self.dpy.intern_atom("_NET_WM_NAME")
        self.A_NET_WM_VISIBLE_NAME = self.dpy.intern_atom("_NET_WM_VISIBLE_NAME")
        self.A_UTF8_STRING = self.dpy.intern_atom("UTF8_STRING")
        # 当前关心的标题 atom 集合（int），用于事件过滤
        self._title_atoms = {self.A_NET_WM_NAME, self.A_NET_WM_VISIBLE_NAME, Xatom.WM_NAME}

        self.active = None          # 当前聚焦窗口 id
        self.active_title = None    # 当前聚焦窗口的最近标题（用于去重）

        # 窗口随时可能消失，吞掉 BadWindow 之类错误别让循环崩。
        self.dpy.set_error_handler(lambda err, req: None)
        self.root.change_attributes(event_mask=X.PropertyChangeMask)

    # ----------------------------- 读属性 -----------------------------
    @staticmethod
    def _decode(value):
        if value is None:
            return None
        if isinstance(value, bytes):
            return value.decode("utf-8", "replace")
        return str(value)

    def get_active_window(self):
        try:
            p = self.root.get_full_property(self.A_NET_ACTIVE_WINDOW, X.AnyPropertyType)
        except error.XError:
            p = None
        if p and p.value:
            return int(p.value[0]) or None
        return None

    def read_title(self, wid):
        """优先 _NET_WM_NAME(UTF8)，退回 WM_NAME；读不到返回 None。"""
        if not wid:
            return None
        win = self.dpy.create_resource_object("window", wid)
        try:
            p = win.get_full_property(self.A_NET_WM_NAME, self.A_UTF8_STRING)
            if p and p.value:
                return self._decode(p.value)
        except error.XError:
            return None
        try:
            p = win.get_full_property(Xatom.WM_NAME, X.AnyPropertyType)
            if p and p.value:
                return self._decode(p.value)
        except error.XError:
            return None
        return None

    def _watch_active_title(self, wid):
        """对新的聚焦窗口 select PropertyChangeMask 并记录其当前标题（不触发回调）。"""
        self.active_title = None
        if not wid:
            return
        win = self.dpy.create_resource_object("window", wid)
        try:
            win.change_attributes(event_mask=X.PropertyChangeMask)
            self.dpy.sync()  # 让 select 立刻生效，避免漏掉紧随其后的标题变化
            self.active_title = self.read_title(wid)
        except error.XError:
            self.active_title = None

    # ----------------------------- 主循环 -----------------------------
    def run(self):
        # 建立基线，但不触发任何回调（启动时既没“切换”也没“标题变化”）
        self.active = self.get_active_window()
        self._watch_active_title(self.active)
        log(f"[focus] 基线焦点窗口: 0x{(self.active or 0):08x}  标题: {self.active_title!r}")

        while True:
            ev = self.dpy.next_event()
            if ev.type != X.PropertyNotify:
                continue

            # (1) root 上的焦点变化
            if ev.window.id == self.root.id and ev.atom == self.A_NET_ACTIVE_WINDOW:
                wid = self.get_active_window()
                if wid is None or wid == self.active:
                    continue
                old, self.active = self.active, wid
                self._watch_active_title(wid)   # 切到新窗口的标题监控（不触发标题回调）
                log(f"[focus] 焦点切换: 0x{(old or 0):08x} -> 0x{wid:08x}  "
                    f"标题: {self.active_title!r}")
                self.on_focus_change(wid, old)
                continue

            # (2) 当前聚焦窗口的标题变化（焦点没变）
            if (ev.window.id == self.active
                    and self.active is not None
                    and ev.atom in self._title_atoms):
                new = self.read_title(self.active)
                if new == self.active_title:
                    continue
                old_title, self.active_title = self.active_title, new
                log(f"[title] 标题变化: 0x{self.active:08x}  {old_title!r} -> {new!r}")
                self.on_title_change(self.active, old_title, new)


def _on_focus_change(_new_wid, _old_wid):
    """焦点切换：当前 segment 立即切断。"""
    _flush_segment("focus_change")


def _on_title_change(_wid, _old_title, _new_title):
    """聚焦窗口标题变化（焦点未变）：当前 segment 立即切断。"""
    _flush_segment("title_change")


# ---------------------------- 演示用：周期打印 buffer ----------------------------
def _buffer_dumper():
    while True:
        time.sleep(BUFFER_DUMP_INTERVAL_S)
        with _lock:
            n = len(_buffer)
            pending = ''.join(_current_chars)
            pending_start = _segment_start_ts
            snapshot = list(_buffer)
        total_chars = sum(len(t) for _, t in snapshot)
        print(file=sys.stderr)
        print("=" * 70, file=sys.stderr)
        print(f"[buffer] total chunks={n}  total chars={total_chars}  "
              f"pending: len={len(pending)} start={_fmt_ts(pending_start)} "
              f"{_preview(pending)}", file=sys.stderr)
        for i, (start_ts, seg) in enumerate(snapshot, 1):
            print(f"  [{i:>3}] start={_fmt_ts(start_ts)}  len={len(seg):>4}  "
                  f"{_preview(seg)}", file=sys.stderr)
        print("=" * 70, file=sys.stderr)


# ---------------------------- 启动 ----------------------------
def _start():
    listener = Listener(on_press=_on_key_press)
    listener.daemon = True
    listener.start()
    log(f"[keyboard] 监听已启动。空闲 {IDLE_TIMEOUT_S}s 或焦点切换即切段。")

    threading.Thread(target=_idle_watcher, name="idle-watcher", daemon=True).start()
    threading.Thread(target=_buffer_dumper, name="buffer-dump", daemon=True).start()

    if _HAVE_XLIB:
        try:
            fm = FocusTitleMonitor(_on_focus_change, _on_title_change)
            threading.Thread(target=fm.run, name="focus-title-monitor", daemon=True).start()
            log("[focus] X11 焦点 + 标题切段已启用。")
        except Exception as exc:
            log(f"[focus] 无法初始化 X11 监听 ({exc})；退化为只按空闲切段。")
    else:
        log("[focus] 未安装 python-xlib；退化为只按空闲切段。")


# ---------------------------- 主程序入口 / Entry point ----------------------------
if __name__ == "__main__":
    _start()
    log("[keyboard] 运行中。Ctrl+C 退出。")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        # 退出前把未完成的最后一段也切出来
        _flush_segment("shutdown")
        print(file=sys.stderr)
        print("=" * 70, file=sys.stderr)
        total_chars = sum(len(t) for _, t in _buffer)
        print(f"[keyboard] 关闭。buffer 最终: total chunks={len(_buffer)}  "
              f"total chars={total_chars}", file=sys.stderr)
        for i, (start_ts, seg) in enumerate(_buffer, 1):
            print(f"  [{i:>3}] start={_fmt_ts(start_ts)}  len={len(seg):>4}  "
                  f"{_preview(seg)}", file=sys.stderr)
        print("=" * 70, file=sys.stderr)
        sys.exit(0)
