#!/usr/bin/env python3
"""
demo-get-window-title-change.py

用 python-xlib 在【单线程 / 单进程】里同时监听：
  1) 当前聚焦窗口变化  —— root 的 _NET_ACTIVE_WINDOW 属性变化
  2) 任意窗口标题变化  —— 各客户端窗口的 _NET_WM_NAME / WM_NAME 属性变化

对应 plan-get-window-title-change.txt 的 Approach A。

为什么不用 xev -root：标题是【客户端窗口】上的属性，root 收不到，所以 xev -root
看不到标题变化。但一个 X 连接可以同时 XSelectInput(PropertyChangeMask) 到 root +
每一个客户端窗口，所有事件都进同一个事件队列，由一个 next_event() 循环读出 ——
无需任何额外线程。

依赖安装（Kubuntu 22.04）：
    sudo apt install python3-xlib
  或
    pip install python-xlib
"""

import sys
import time

try:
    from Xlib import X, display, Xatom, error
except ImportError:
    sys.stderr.write(
        "缺少 python-xlib。请安装:\n"
        "    sudo apt install python3-xlib\n"
        "  或\n"
        "    pip install python-xlib\n"
    )
    sys.exit(1)


def log(msg):
    """带时间戳、立即 flush 的输出。"""
    print(f"{time.strftime('%H:%M:%S')} | {msg}", flush=True)


def wid_str(wid):
    """把整数窗口 id 归一化成 wmctrl 形式：0x05400007。"""
    return f"0x{wid:08x}"


class TitleFocusMonitor:
    # root 上这些属性变化 => 焦点 / 客户端列表 变化
    ROOT_ATOMS_FOCUS = {"_NET_ACTIVE_WINDOW"}
    ROOT_ATOMS_LIST = {"_NET_CLIENT_LIST", "_NET_CLIENT_LIST_STACKING"}
    # 客户端窗口上这些属性变化 => 标题变化
    TITLE_ATOMS = {"_NET_WM_NAME", "WM_NAME", "_NET_WM_VISIBLE_NAME"}

    def __init__(self):
        self.dpy = display.Display()
        self.root = self.dpy.screen().root

        # 预先 intern 需要的 atom
        self.A_NET_CLIENT_LIST = self.dpy.intern_atom("_NET_CLIENT_LIST")
        self.A_NET_CLIENT_LIST_STACKING = self.dpy.intern_atom("_NET_CLIENT_LIST_STACKING")
        self.A_NET_ACTIVE_WINDOW = self.dpy.intern_atom("_NET_ACTIVE_WINDOW")
        self.A_NET_WM_NAME = self.dpy.intern_atom("_NET_WM_NAME")
        self.A_UTF8_STRING = self.dpy.intern_atom("UTF8_STRING")

        # 状态
        self.titles = {}      # wid(int) -> 最近标题
        self.watched = set()  # 已 select 了 PropertyChangeMask 的窗口集合
        self.active = None    # 最近的活动窗口 id
        self._atom_name_cache = {}

        # 全局错误处理：窗口随时可能消失，忽略 BadWindow / BadAtop 之类，别让循环崩。
        self.dpy.set_error_handler(self._on_x_error)

        # 监听 root，以拿到焦点 / 客户端列表 / 桌面等变化
        self.root.change_attributes(event_mask=X.PropertyChangeMask)

    # ----------------------------- 工具 -----------------------------
    def _on_x_error(self, err, request):
        # 仅在调试时关心；常见是对刚关闭窗口的 BadWindow，安全忽略。
        log(f"[x-error 已忽略] {err}")

    def _atom_name(self, atom):
        if atom in self._atom_name_cache:
            return self._atom_name_cache[atom]
        name = ""
        if atom:
            try:
                name = self.dpy.get_atom_name(atom)
            except error.XError:
                name = ""
        self._atom_name_cache[atom] = name
        return name

    @staticmethod
    def _decode(value):
        if value is None:
            return None
        if isinstance(value, bytes):
            return value.decode("utf-8", "replace")
        return str(value)

    def _win(self, wid):
        return self.dpy.create_resource_object("window", wid)

    def read_title(self, win):
        """优先 _NET_WM_NAME(UTF8)，退回 WM_NAME；读不到返回 None。"""
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

    def get_client_list(self):
        """返回当前所有客户端窗口 id（int 列表）。"""
        for atom in (self.A_NET_CLIENT_LIST, self.A_NET_CLIENT_LIST_STACKING):
            try:
                p = self.root.get_full_property(atom, X.AnyPropertyType)
            except error.XError:
                p = None
            if p and p.value:
                return list(p.value)
        return []

    def get_active_window(self):
        """返回当前活动窗口 id（int），没有则 None。"""
        try:
            p = self.root.get_full_property(self.A_NET_ACTIVE_WINDOW, X.AnyPropertyType)
        except error.XError:
            p = None
        if p and p.value:
            wid = int(p.value[0])
            return wid or None
        return None

    # ----------------------------- 监听管理 -----------------------------
    def watch(self, wid):
        """对一个客户端窗口 select PropertyChangeMask，并记录其当前标题。"""
        if wid in self.watched:
            return
        win = self._win(wid)
        try:
            win.change_attributes(event_mask=X.PropertyChangeMask)
            title = self.read_title(win)
        except error.XError:
            return  # 窗口可能已消失
        self.watched.add(wid)
        self.titles[wid] = title

    def sync_clients(self, initial=False):
        """根据 _NET_CLIENT_LIST 调和被监听窗口集合：新增的开始监听，消失的丢弃。"""
        current = set(self.get_client_list())
        added = current - self.watched
        gone = self.watched - current
        for wid in added:
            self.watch(wid)
        for wid in gone:
            self.watched.discard(wid)
            self.titles.pop(wid, None)
        self.dpy.sync()  # 让 change_attributes 立刻生效
        if initial:
            log(f"开始监听 {len(self.watched)} 个窗口的标题变化。")
        elif added or gone:
            log(f"窗口集合更新: +{len(added)} 新增, -{len(gone)} 关闭 "
                f"(当前监听 {len(self.watched)} 个)")

    # ----------------------------- 事件处理 -----------------------------
    def report_active(self, initial=False):
        """解析并报告当前活动（聚焦）窗口；去重，仅在变化时打印。"""
        wid = self.get_active_window()
        if not wid:
            return
        if wid == self.active and not initial:
            return
        self.active = wid
        self.watch(wid)  # 确保也在监听它的标题
        title = self.titles.get(wid) or self.read_title(self._win(wid))
        tag = "当前聚焦窗口(基线)" if initial else "焦点切换"
        log(f"  ★ {tag}: {wid_str(wid)}  标题: {title!r}")

    def handle_title_change(self, win, state):
        """某客户端窗口标题属性变化：读取新标题，仅在与旧值不同的时候报告。"""
        wid = win.id
        new = self.read_title(win)
        old = self.titles.get(wid)
        if new == old:
            return
        self.titles[wid] = new
        focus_mark = " (当前聚焦)" if wid == self.active else ""
        if new is None:
            log(f"  ✎ 标题被清除: {wid_str(wid)}{focus_mark}  旧: {old!r}")
        else:
            log(f"  ✎ 标题变化: {wid_str(wid)}{focus_mark}  {old!r} -> {new!r}")

    # ----------------------------- 主循环 -----------------------------
    def run(self):
        log("=== 窗口标题 / 焦点 监听器启动 (python-xlib, 单线程) ===")
        self.sync_clients(initial=True)
        self.report_active(initial=True)
        log("-" * 60)

        while True:
            ev = self.dpy.next_event()
            if ev.type != X.PropertyNotify:
                continue

            name = self._atom_name(ev.atom)
            if not name:
                continue
            ev_wid = ev.window.id

            if ev_wid == self.root.id:
                # root 上的属性：焦点 / 客户端列表
                if name in self.ROOT_ATOMS_FOCUS:
                    self.report_active()
                elif name in self.ROOT_ATOMS_LIST:
                    self.sync_clients()
            else:
                # 客户端窗口上的属性：只关心标题相关 atom
                if name in self.TITLE_ATOMS:
                    self.handle_title_change(ev.window, ev.state)


def main():
    try:
        monitor = TitleFocusMonitor()
    except Exception as exc:  # 连接不上 X 等
        log(f"无法初始化监听器: {exc}")
        log("提示: 需要 X11 会话 (echo $XDG_SESSION_TYPE 应为 x11)。")
        sys.exit(1)

    try:
        monitor.run()
    except KeyboardInterrupt:
        log("")
        log("收到 Ctrl-C，退出。")
    finally:
        try:
            monitor.dpy.close()
        except Exception:
            pass
        log("=== 监听结束 ===")


if __name__ == "__main__":
    main()
