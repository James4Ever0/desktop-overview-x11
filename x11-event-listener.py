#!/usr/bin/env python3
"""
X11 事件监听器 —— 监听 root 窗口的 focus / property 事件，并解析出“当前活动窗口”。

要点（回答“xev 能不能直接给出窗口 id 和标题”）：
  xev 只会告诉你 “root 窗口的 _NET_ACTIVE_WINDOW 属性变了”，它本身**不**给出新的
  活动窗口 id，也不给标题。输出里的 0x831 是 root 窗口自己，不是被聚焦的应用。
  所以拿到“变化”信号后，必须再发起一次查询：
     xprop -root _NET_ACTIVE_WINDOW   -> 活动窗口 id（16 进制）
     -> 归一化成 wmctrl 的 0x0XXXXXXX 形式
     -> 在 wmctrl -l 里查这个 id 的标题
  这一步**不需要单独线程**：查询很快、事件不频繁，查询期间 xev 的输出由内核管道
  缓冲着不会丢；同时给查询加了 timeout，绝不会卡住读取循环。并对重复的
  _NET_ACTIVE_WINDOW（每次切换会连发 2 条）做了去重，只在 id/标题真正变化时才报。

  局限：监听 -root 只能看到 root 的属性（活动窗口、客户端列表等），看不到某个具体
  窗口“自己改标题”(_NET_WM_NAME)。要跟踪某个窗口自身的标题变化，得再对那个窗口
  开一个 `xev -id <win>`（那才是需要额外线程/进程的场景）——需要的话我可以加。

读取相关改动：用 iter(readline,'') + bufsize=1，逐行即时读取，绝不被预读缓冲攒批。
"""

import re
import sys
import shutil
import subprocess

# 是否回显 xev 的每一行原始输出（跟踪“到底收到了什么”）
ECHO_RAW = True

CMD = ['xev', '-root', '-event', 'focus', '-event', 'property']

# 这些 root 属性变化 => 需要重新解析“当前活动窗口”
TRIGGER_ATOMS = {"_NET_ACTIVE_WINDOW"}

# xev 事件头：  "PropertyNotify event, serial 18, synthetic NO, window 0x831,"
HEADER_RE = re.compile(
    r"^(\w+) event, serial \d+, synthetic (?:YES|NO), window (0x[0-9a-fA-F]+)"
)
# atom 行：     "atom 0x19b (_NET_ACTIVE_WINDOW), time 798774354, state PropertyNewValue"
ATOM_RE = re.compile(r"atom (0x[0-9a-fA-F]+) \(([^)]*)\)")
STATE_RE = re.compile(r"state (\w+)")
# xprop -root _NET_ACTIVE_WINDOW => "_NET_ACTIVE_WINDOW(WINDOW): window id # 0x5400007"
XPROP_ACTIVE_RE = re.compile(r"window id # (0x[0-9a-fA-F]+)")


def log(msg):
    """统一、立即 flush 的输出（管道下也不被缓冲）。"""
    print(msg, flush=True)


def _run(cmd, timeout=2.0):
    """跑一个查询型命令，返回 stdout 文本；缺命令/超时/出错都返回 None（绝不抛、不卡）。"""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        return None
    except subprocess.TimeoutExpired:
        log(f"     [warn] 命令超时: {' '.join(cmd)}")
        return None
    if p.returncode != 0:
        return None
    return p.stdout


def normalize_wid(raw, base=16):
    """把窗口 id 归一化成 wmctrl 的形式：小写、补零到 8 位，如 0x05400007。"""
    try:
        return f"0x{int(raw, base):08x}"
    except (ValueError, TypeError):
        return None


def get_active_window_id():
    """读取当前活动窗口 id（归一化为 wmctrl 形式）。优先 xprop，退路 xdotool。"""
    out = _run(['xprop', '-root', '_NET_ACTIVE_WINDOW'])
    if out:
        m = XPROP_ACTIVE_RE.search(out)
        if m:
            return normalize_wid(m.group(1), base=16)
    # 退路：xdotool 输出 10 进制 id
    out = _run(['xdotool', 'getactivewindow'])
    if out and out.strip().isdigit():
        return normalize_wid(out.strip(), base=10)
    return None


def wmctrl_title_map():
    """解析 wmctrl -l，返回 {归一化id: 标题}。"""
    mapping = {}
    out = _run(['wmctrl', '-l'])
    if not out:
        return mapping
    for line in out.splitlines():
        parts = line.split(maxsplit=3)      # id  desktop  host  title
        if len(parts) >= 4:
            wid = normalize_wid(parts[0], base=16)
            if wid:
                mapping[wid] = parts[3]
    return mapping


def get_title(wid):
    """取某窗口标题：先在 wmctrl -l 里查（与用户期望的 id/标题一致），退路用 xdotool。"""
    title = wmctrl_title_map().get(wid)
    if title is not None:
        return title
    out = _run(['xdotool', 'getwindowname', str(int(wid, 16))])
    if out:
        return out.strip()
    return "<标题未知 / 不在 wmctrl 列表中>"


def resolve_active(state, reason):
    """查询当前活动窗口；仅在 id 或 标题 真正变化时才打印（去重）。"""
    wid = get_active_window_id()
    if not wid or wid == "0x00000000":
        return
    title = get_title(wid)

    changed_id = wid != state['id']
    changed_title = title != state['title']
    if not (changed_id or changed_title):
        log(f"     （活动窗口未变化，仍是 {wid}，忽略）")
        return

    what = []
    if changed_id:
        what.append("窗口")
    if changed_title:
        what.append("标题")
    log("")
    log(f"  ★★ 活动{'/'.join(what)}变化  (触发: {reason}) ★★")
    log(f"       window id : {wid}        <- 与 `wmctrl -l` 中的 id 同一格式")
    log(f"       title     : {title}")
    log(f"       上一个     : id={state['id']}  title={state['title']!r}")
    log("")
    state['id'] = wid
    state['title'] = title


def check_tools():
    """启动时报告可用的查询工具，方便排查。"""
    for name in ('xev', 'xprop', 'xdotool', 'wmctrl'):
        path = shutil.which(name)
        log(f"  工具 {name:<8} -> {path or '缺失!'}")
    if not shutil.which('xev'):
        log("致命: 缺少 xev，无法监听。请安装 x11-utils。")
        sys.exit(1)
    if not (shutil.which('xprop') or shutil.which('xdotool')):
        log("警告: xprop 和 xdotool 都缺失，将无法解析活动窗口 id（仅能回显事件）。")


def main():
    log("=== X11 事件监听器启动 ===")
    log("将执行: " + " ".join(CMD))
    check_tools()

    # 先打印一次“当前活动窗口”作为基线
    active_state = {'id': None, 'title': None}
    resolve_active(active_state, reason="启动基线")

    process = subprocess.Popen(
        CMD,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,   # 合并 stderr，确保读到 xev 的一切
        text=True,
        bufsize=1,                  # 行缓冲
    )
    log(f"xev 已启动 (pid={process.pid})。逐行即时读取 (readline)...")
    log("-" * 64)

    line_count = 0
    event_count = 0
    cur_event = None
    cur_window = None

    try:
        for raw in iter(process.stdout.readline, ''):
            line_count += 1
            line = raw.rstrip("\n")

            if ECHO_RAW:
                log(f"RAW[{line_count:>5}]: {line!r}")

            stripped = line.strip()
            if not stripped:
                continue

            # 1) 事件头：记下事件类型 + 窗口；focus 事件直接报出。
            h = HEADER_RE.match(stripped)
            if h:
                cur_event = h.group(1)
                cur_window = h.group(2)
                event_count += 1
                tag = "焦点事件" if cur_event in ("FocusIn", "FocusOut") else "事件"
                log(f"  -> {tag} #{event_count}: {cur_event}  window={cur_window}")
                continue

            # 2) atom 行：属于上面那条事件头。
            a = ATOM_RE.search(stripped)
            if a:
                atom_id, atom_name = a.group(1), a.group(2)
                st = STATE_RE.search(stripped)
                atom_state = st.group(1) if st else "?"
                log(f"     属性变化: window={cur_window} "
                    f"atom={atom_id} ({atom_name}) state={atom_state}")

                # 关键：这个属性意味着活动窗口可能变了 -> 去查实际 id + 标题。
                if atom_name in TRIGGER_ATOMS:
                    log(f"     -> {atom_name} 变化，正在解析当前活动窗口...")
                    resolve_active(active_state, reason=f"{atom_name} on {cur_window}")
                continue

        # EOF
        log("-" * 64)
        log(f"!! xev 的 stdout 已关闭 (EOF)。共读取 {line_count} 行 / {event_count} 个事件。")
        log("   xev 进程可能已退出（被杀、崩溃或显示器断开）。")

    except KeyboardInterrupt:
        log("")
        log("收到 Ctrl-C，正在停止监听...")
    finally:
        rc = process.poll()
        if rc is None:
            log("正在终止 xev 子进程...")
            process.terminate()
            try:
                rc = process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                log("xev 未在 3s 内退出，强制 kill。")
                process.kill()
                rc = process.wait()
        log(f"xev 退出码: {rc}")
        log(f"统计: 共读取 {line_count} 行, 解析出 {event_count} 个事件。")
        log("=== 监听结束 ===")


if __name__ == "__main__":
    main()
