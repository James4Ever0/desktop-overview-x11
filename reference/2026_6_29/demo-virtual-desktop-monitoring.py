#!/usr/bin/env python3
"""
demo-virtual-desktop-monitoring.py

对应 plan-virtual-desktop.txt：虚拟桌面的【监听 / 读取 / 切换】三件事，三个子命令：

    python3 demo-virtual-desktop-monitoring.py list           # 列出所有桌面(id+名字+当前)
    python3 demo-virtual-desktop-monitoring.py switch <N>      # 切换到桌面 N (0 起)
    python3 demo-virtual-desktop-monitoring.py monitor         # 监听桌面切换事件

monitor 做的事（单线程 / 单进程，python-xlib，事件驱动，无轮询）：
  - 启动先打印【初始桌面 id + 名字】基线。
  - 监听 root 的 _NET_CURRENT_DESKTOP 变化 => 解析【目标桌面 id + 名字】，去重后打印
        ★ 虚拟桌面切换: <old id>(<old name>) -> <new id>(<new name>)
  - 顺带监听 _NET_NUMBER_OF_DESKTOPS（增删）/ _NET_DESKTOP_NAMES（重命名）。

为什么 monitor 不用看客户端窗口：与窗口标题不同，_NET_CURRENT_DESKTOP 是 ROOT 上的
属性，所以一个 X 连接、一个 next_event() 循环就能事件驱动地拿到桌面切换 —— 不像标题
那样需要去 select 每个客户端窗口（见 plan-get-window-title-change.txt）。

切换(switch)用 wmctrl -s N（退路 xdotool set_desktop N）。

依赖（Kubuntu 22.04 / KDE Plasma / X11）：
    sudo apt install python3-xlib wmctrl x11-utils xdotool
  python-xlib 也可: pip install python-xlib
注意：仅限 X11。echo $XDG_SESSION_TYPE 必须是 x11；Wayland 下全部无效。
"""

import sys
import time
import shutil
import subprocess

try:
    from Xlib import X, display, error
except ImportError:
    sys.stderr.write(
        "缺少 python-xlib。请安装:\n"
        "    sudo apt install python3-xlib\n"
        "  或\n"
        "    pip install python-xlib\n"
    )
    sys.exit(1)


STICKY = 0xFFFFFFFF  # _NET_WM_DESKTOP 的“所有桌面/粘滞”值（这里桌面索引一般用不到）


def log(msg):
    """带时间戳、立即 flush 的输出。"""
    print(f"{time.strftime('%H:%M:%S')} | {msg}", flush=True)


def _run(cmd, timeout=2.0):
    """跑一个外部命令，返回 stdout 文本；缺命令/超时/非零退出都返回 None（绝不抛）。"""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        return None
    except subprocess.TimeoutExpired:
        log(f"  [warn] 命令超时: {' '.join(cmd)}")
        return None
    if p.returncode != 0:
        return None
    return p.stdout


class VirtualDesktopMonitor:
    # root 上这些属性变化是我们关心的
    A_CURRENT = "_NET_CURRENT_DESKTOP"
    A_COUNT = "_NET_NUMBER_OF_DESKTOPS"
    A_NAMES = "_NET_DESKTOP_NAMES"
    WATCH_ATOMS = {A_CURRENT, A_COUNT, A_NAMES}

    def __init__(self):
        self.dpy = display.Display()
        self.root = self.dpy.screen().root

        # 预先 intern atom
        self.A_NET_CURRENT_DESKTOP = self.dpy.intern_atom(self.A_CURRENT)
        self.A_NET_NUMBER_OF_DESKTOPS = self.dpy.intern_atom(self.A_COUNT)
        self.A_NET_DESKTOP_NAMES = self.dpy.intern_atom(self.A_NAMES)
        self.A_UTF8_STRING = self.dpy.intern_atom("UTF8_STRING")

        self._atom_name_cache = {}
        self.current = None   # 最近一次已知的当前桌面索引
        self.count = None
        self.names = []

        self.dpy.set_error_handler(self._on_x_error)

    # ----------------------------- 工具 -----------------------------
    def _on_x_error(self, err, request):
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

    # ----------------------------- 读取（python-xlib 优先，外部命令退路）-----------------------------
    def get_current_desktop(self):
        """当前桌面索引(int)；读不到返回 None。"""
        try:
            p = self.root.get_full_property(self.A_NET_CURRENT_DESKTOP, X.AnyPropertyType)
            if p and p.value:
                return int(p.value[0])
        except error.XError:
            pass
        # 退路：wmctrl -d 里带 '*' 的那行
        for idx, _name, active in self._wmctrl_desktops():
            if active:
                return idx
        return None

    def get_number_of_desktops(self):
        try:
            p = self.root.get_full_property(self.A_NET_NUMBER_OF_DESKTOPS, X.AnyPropertyType)
            if p and p.value:
                return int(p.value[0])
        except error.XError:
            pass
        rows = self._wmctrl_desktops()
        return len(rows) if rows else None

    def get_desktop_names(self):
        """桌面名字列表（按索引）。KDE 可能比真实桌面数多，调用方自行 clamp。"""
        try:
            p = self.root.get_full_property(self.A_NET_DESKTOP_NAMES, X.AnyPropertyType)
            if p and p.value:
                raw = p.value
                if isinstance(raw, bytes):
                    parts = raw.split(b"\x00")
                    return [s.decode("utf-8", "replace") for s in parts if s != b""]
        except error.XError:
            pass
        # 退路：wmctrl -d 的名字列
        return [name for _idx, name, _active in self._wmctrl_desktops()]

    @staticmethod
    def _wmctrl_desktops():
        """解析 wmctrl -d -> [(index, name, active_bool), ...]；失败返回 []。"""
        out = _run(['wmctrl', '-d'])
        rows = []
        if not out:
            return rows
        for line in out.splitlines():
            parts = line.split(maxsplit=9)  # idx active DG.. VP.. WA.. x,y WxH name
            if len(parts) < 2:
                continue
            try:
                idx = int(parts[0])
            except ValueError:
                continue
            active = parts[1] == '*'
            name = parts[-1] if len(parts) >= 1 else f"Desktop {idx + 1}"
            rows.append((idx, name, active))
        return rows

    def name_for(self, idx):
        """桌面索引 -> 名字（越界给个合成名）。"""
        if idx is None:
            return "?"
        if 0 <= idx < len(self.names):
            return self.names[idx]
        return f"Desktop {idx + 1}"

    def refresh_state(self):
        """刷新 count / names / current 缓存。"""
        self.count = self.get_number_of_desktops()
        self.names = self.get_desktop_names()
        self.current = self.get_current_desktop()

    # ----------------------------- 子命令: list -----------------------------
    def cmd_list(self):
        self.refresh_state()
        cnt = self.count if self.count is not None else len(self.names)
        log(f"虚拟桌面共 {cnt} 个，当前活动: "
            f"{self.current} ({self.name_for(self.current)})")
        # KDE 怪癖：names 可能比真实桌面数多，按 count clamp 显示
        show_n = cnt if cnt else len(self.names)
        for idx in range(show_n):
            mark = " *" if idx == self.current else "  "
            log(f" {mark} [{idx}] {self.name_for(idx)}")
        # 若名字数量超出真实桌面数，提示一下（KDE 预置多余名字）
        if len(self.names) > show_n:
            extra = ", ".join(self.names[show_n:])
            log(f"  (另有 {len(self.names) - show_n} 个预置但未启用的名字: {extra})")

    # ----------------------------- 子命令: switch -----------------------------
    def cmd_switch(self, target):
        self.refresh_state()
        cnt = self.count if self.count is not None else len(self.names)
        if cnt and not (0 <= target < cnt):
            log(f"目标桌面 {target} 越界：有效范围 0..{cnt - 1}。")
            return 2
        old = self.current
        log(f"切换桌面: {old} ({self.name_for(old)}) -> "
            f"{target} ({self.name_for(target)}) ...")

        ok = False
        if shutil.which('wmctrl'):
            ok = _run(['wmctrl', '-s', str(target)]) is not None
        if not ok and shutil.which('xdotool'):
            log("  wmctrl 失败/缺失，改用 xdotool set_desktop ...")
            ok = _run(['xdotool', 'set_desktop', str(target)]) is not None
        if not ok:
            log("切换失败：wmctrl 与 xdotool 均不可用或返回错误。")
            return 1

        # 切换后立即回读确认（plan: 切后立刻读可能短暂返回旧值，再读一次）
        time.sleep(0.15)
        now = self.get_current_desktop()
        if now == target:
            log(f"✓ 已切换到桌面 {now} ({self.name_for(now)})。")
            return 0
        log(f"已发出切换命令，但回读到的是 {now} ({self.name_for(now)})；"
            f"可能稍后才生效，或 WM 拒绝了切换。")
        return 0

    # ----------------------------- 子命令: monitor -----------------------------
    def cmd_monitor(self):
        log("=== 虚拟桌面监听器启动 (python-xlib, 单线程, 事件驱动) ===")
        self.refresh_state()
        cnt = self.count if self.count is not None else len(self.names)
        # 初始桌面 id + 名字 基线
        log(f"初始桌面: id={self.current}  name={self.name_for(self.current)!r}  "
            f"(共 {cnt} 个桌面)")
        log(f"全部桌面: " + " | ".join(
            f"[{i}]{self.name_for(i)}" for i in range(cnt or len(self.names))))
        log("提示: 切换桌面（或在另一终端 `wmctrl -s N`）即可看到事件。Ctrl-C 退出。")
        log("-" * 60)

        # 监听 root 的属性变化
        self.root.change_attributes(event_mask=X.PropertyChangeMask)
        self.dpy.sync()

        while True:
            ev = self.dpy.next_event()
            if ev.type != X.PropertyNotify:
                continue
            if ev.window.id != self.root.id:
                continue
            name = self._atom_name(ev.atom)
            if name not in self.WATCH_ATOMS:
                continue

            if name == self.A_CURRENT:
                self._on_current_changed()
            elif name == self.A_COUNT:
                self._on_count_changed()
            elif name == self.A_NAMES:
                self._on_names_changed()

    def _on_current_changed(self):
        new = self.get_current_desktop()
        old = self.current
        if new is None or new == old:
            return  # 去重：连发的重复事件忽略
        self.current = new
        log(f"  ★ 虚拟桌面切换: "
            f"{old} ({self.name_for(old)!r}) -> {new} ({self.name_for(new)!r})")

    def _on_count_changed(self):
        new = self.get_number_of_desktops()
        if new == self.count:
            return
        old = self.count
        self.count = new
        self.names = self.get_desktop_names()  # 数量变了，名字也刷新
        log(f"  ◇ 桌面数量变化: {old} -> {new}")

    def _on_names_changed(self):
        old = self.names
        new = self.get_desktop_names()
        if new == old:
            return
        self.names = new
        log(f"  ◇ 桌面名字更新: {new}")


USAGE = (
    "用法:\n"
    "  python3 demo-virtual-desktop-monitoring.py list           列出所有虚拟桌面 (id+名字+当前)\n"
    "  python3 demo-virtual-desktop-monitoring.py switch <N>     切换到桌面 N (0 起)\n"
    "  python3 demo-virtual-desktop-monitoring.py monitor        监听桌面切换事件\n"
)


def main():
    argv = sys.argv[1:]
    if not argv or argv[0] in ("-h", "--help", "help"):
        sys.stdout.write(USAGE)
        return 0

    cmd = argv[0]
    try:
        mon = VirtualDesktopMonitor()
    except Exception as exc:
        log(f"无法连接 X / 初始化: {exc}")
        log("提示: 需要 X11 会话 (echo $XDG_SESSION_TYPE 应为 x11)。")
        return 1

    try:
        if cmd == "list":
            mon.cmd_list()
            return 0
        elif cmd == "switch":
            if len(argv) < 2 or not argv[1].lstrip("-").isdigit():
                log("switch 需要一个桌面索引，例如: switch 1")
                return 2
            return mon.cmd_switch(int(argv[1]))
        elif cmd == "monitor":
            try:
                mon.cmd_monitor()
            except KeyboardInterrupt:
                log("")
                log("收到 Ctrl-C，退出。")
            return 0
        else:
            sys.stdout.write(USAGE)
            return 2
    finally:
        try:
            mon.dpy.close()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
