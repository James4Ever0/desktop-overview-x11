#!/usr/bin/env python3
"""
Standalone global keyboard event listener.

Captures printable/visible keypresses (chars, space, tab, enter, backspace)
with timestamps and dumps the aggregated buffer to stderr at a configurable
interval. Buffer accumulates forever — never cleared.

Dependencies: pip install pynput
"""

import sys
import time
import threading
from datetime import datetime
from pynput.keyboard import Listener, Key, KeyCode

# ---------------------------- 配置 / Config ----------------------------
KEY_BUFFER_DUMP_INTERVAL_S = 10   # dump aggregated buffer every N seconds

# ---------------------------- 全局缓冲 / Global buffer ----------------------------
# 线程安全的缓冲区：存储 (timestamp, key_repr) 元组
_key_buffer = []
_buffer_lock = threading.Lock()


def _key_to_repr(key):
    """将 pynput key 转换为可打印表示；非可见键返回 None。"""
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


def _on_key_press(key):
    """pynput listener 回调——将可见按键记录到缓冲区。"""
    ts = time.time()
    rep = _key_to_repr(key)
    if rep is None:
        return True   # 继续监听，忽略非可见键

    with _buffer_lock:
        print("Received key at time %s: %s" % (ts, repr(rep)))
        _key_buffer.append((ts, rep))
    return True       # 始终返回 True 以保持监听器运行


def _dump_key_buffer():
    """守护线程：每隔 KEY_BUFFER_DUMP_INTERVAL_S 秒打印一次缓冲区全部内容。"""
    while True:
        time.sleep(KEY_BUFFER_DUMP_INTERVAL_S)
        with _buffer_lock:
            if not _key_buffer:
                print("[keyboard] No keys pressed in this interval.", file=sys.stderr)
                continue
            print(file=sys.stderr)
            print("=" * 70, file=sys.stderr)
            print(f"[keyboard] Buffer dump ({len(_key_buffer)} keys accumulated so far):",
                  file=sys.stderr)
            for ts, rep in _key_buffer:
                dt = datetime.fromtimestamp(ts)
                print(f"  {dt.strftime('%H:%M:%S.%f')[:-3]}  {rep!r}", file=sys.stderr)
            print("=" * 70, file=sys.stderr)


def _start_listener():
    """启动全局键盘监听器和定时打印线程（均为守护线程）。"""
    listener = Listener(on_press=_on_key_press)
    listener.daemon = True
    listener.start()
    print(f"[keyboard] Listener started. Dumping buffer every {KEY_BUFFER_DUMP_INTERVAL_S}s.",
          file=sys.stderr)

    dumper = threading.Thread(target=_dump_key_buffer, name="key-dump", daemon=True)
    dumper.start()


# ---------------------------- 主程序入口 / Entry point ----------------------------
if __name__ == "__main__":
    _start_listener()
    print("[keyboard] Running. Press Ctrl+C to stop.", file=sys.stderr)

    try:
        # 保持主线程存活，所有工作由守护线程执行
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        # 退出前做一次最终 dump（可选）
        print(file=sys.stderr)
        print("=" * 70, file=sys.stderr)
        print("[keyboard] Shutting down. Final buffer dump:", file=sys.stderr)
        with _buffer_lock:
            if _key_buffer:
                for ts, rep in _key_buffer:
                    dt = datetime.fromtimestamp(ts)
                    print(f"  {dt.strftime('%H:%M:%S.%f')[:-3]}  {rep!r}", file=sys.stderr)
            else:
                print("  (buffer empty)", file=sys.stderr)
        print("=" * 70, file=sys.stderr)
        sys.exit(0)
