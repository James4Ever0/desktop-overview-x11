#!/usr/bin/env python3
"""
Quick & Dirty Window Preview Tool for Kubuntu 22.04 (X11) — improved edition

Dependencies: wmctrl, import (ImageMagick), xdotool, python3-tk, Pillow

Improvements over demo.py:
  1. Detailed console logging of every step (via the `logging` module).
  2. Faster refresh: window screenshots are captured concurrently using
     asyncio + loop.run_in_executor instead of one-at-a-time.

Power-efficient refresh (this variant):
  Instead of re-capturing *every* window on each refresh (the full-update
  approach), each tick (every REFRESH_INTERVAL_S seconds) we capture only:
    - the currently focused/active window (always, so it stays fresh), plus
    - up to REFRESH_BATCH_SIZE background windows, drawn from a shuffled queue.
  Background ids are enqueued and shuffled; each tick pops a batch (skipping
  ids that have since disappeared). When the queue runs out, we collect all
  current background ids, shuffle again, and refill. Over several ticks every
  window is refreshed in turn, but each individual tick does only a small,
  bounded amount of capture work — far cheaper than a full sweep.
"""

import os
import sys
import io
import time
import random
import asyncio
import logging
import threading
import subprocess
import tkinter as tk
from tkinter import ttk
import tkinter.font as tkfont
from concurrent.futures import ThreadPoolExecutor

from PIL import Image, ImageTk

# ---------------------------- 日志配置 / Logging ----------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(threadName)-12s | %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger("window-preview")

# tesseract 是否可用（启动时检测；缺失则自动跳过 OCR，不影响截图与搜索）
_OCR_AVAILABLE = True


# ---------------------------- 依赖检查 / Dependencies ----------------------------
def check_dependencies():
    log.info("Checking required external commands: wmctrl, import, xdotool ...")
    missing = []
    for cmd in ['wmctrl', 'import', 'xdotool']:
        found = subprocess.call(
            ['which', cmd], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        ) == 0
        log.info("  dependency %-8s -> %s", cmd, "OK" if found else "MISSING")
        if not found:
            missing.append(cmd)

    if missing:
        log.error("Missing dependencies: %s", ", ".join(missing))
        print("=" * 60, file=sys.stderr)
        print("ERROR: Missing required dependencies:", ", ".join(missing), file=sys.stderr)
        print(file=sys.stderr)
        print("Please install them with:", file=sys.stderr)
        if 'wmctrl' in missing:
            print("  sudo apt install wmctrl", file=sys.stderr)
        if 'import' in missing:
            print("  sudo apt install imagemagick", file=sys.stderr)
        if 'xdotool' in missing:
            print("  sudo apt install xdotool", file=sys.stderr)
        print("=" * 60, file=sys.stderr)
        return False

    log.info("All dependencies satisfied.")

    # tesseract 是可选项：缺失时仅禁用 OCR，不阻止程序运行。
    if OCR_ENABLED:
        global _OCR_AVAILABLE
        _OCR_AVAILABLE = subprocess.call(
            ['which', 'tesseract'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        ) == 0
        if _OCR_AVAILABLE:
            log.info("  optional OCR    -> OK (tesseract, lang=%s)", OCR_LANG)
        else:
            log.warning("  optional OCR    -> MISSING (install: sudo apt install tesseract-ocr "
                        "tesseract-ocr-chi-sim); OCR search disabled.")
    return True


# ---------------------------- 窗口操作函数 / Window ops ----------------------------
def get_window_list():
    """通过 wmctrl -l 获取窗口列表，返回 [(id, title), ...]"""
    cmd = ['wmctrl', '-l']
    log.info("Running: %s", " ".join(cmd))
    proc = subprocess.run(cmd, capture_output=True, text=True)
    stderr = (proc.stderr or "").strip()
    if proc.returncode != 0:
        log.error("wmctrl exited %d. stderr: %s", proc.returncode, stderr or "<empty>")
        return []
    if stderr:
        log.warning("wmctrl stderr (non-fatal): %s", stderr)

    output = proc.stdout
    windows = []
    for line in output.strip().split('\n'):
        if not line:
            continue
        parts = line.strip().split(maxsplit=3)
        if len(parts) >= 4:
            win_id = parts[0]          # 如 0x03a00003
            title = parts[3]           # 窗口标题
            windows.append((win_id, title))

    log.info("Found %d window(s).", len(windows))
    for win_id, title in windows:
        log.debug("  window %s -> %s", win_id, title)
    return windows


def capture_window(win_id, title=""):
    """使用 import -window 截图，返回 PIL Image 对象，失败返回 None。

    NOTE: 这是一个阻塞调用 (blocking). 它会在线程池中被并发执行。

    `import` (ImageMagick) 底层通过 X11 的 XGetImage 抓取窗口像素。
    如果窗口当前不可见（最小化/在其它虚拟桌面/未映射），XGetImage 会失败，
    `import` 返回非零退出码 —— 这就是部分窗口截图失败的根本原因。
    我们在这里完整地记录 stderr，方便排查。
    """
    cmd = ['import', '-window', win_id, 'png:-']
    t0 = time.perf_counter()
    log.info("Capturing %s (%s) -> %s", win_id, title[:40], " ".join(cmd))

    proc = subprocess.run(cmd, capture_output=True)
    elapsed = (time.perf_counter() - t0) * 1000
    stderr = proc.stderr.decode('utf-8', 'replace').strip()

    if proc.returncode != 0:
        log.warning(
            "  capture FAILED for %s (%s) after %.0f ms | exit=%d | stderr: %s",
            win_id, title[:40], elapsed, proc.returncode, stderr or "<empty>",
        )
        return None

    if stderr:
        log.warning("  capture %s produced stderr (non-fatal): %s", win_id, stderr)

    try:
        img = Image.open(io.BytesIO(proc.stdout))
        img.load()  # force decode now, while we're off the main thread
    except Exception as exc:
        log.warning(
            "  decode FAILED for %s after %.0f ms: %s (got %d bytes of png data)",
            win_id, elapsed, exc, len(proc.stdout),
        )
        return None

    log.info("  captured %s in %.0f ms (%dx%d)", win_id, elapsed, img.width, img.height)
    return img


def get_app_name(win_id):
    """通过 xprop WM_CLASS 获取窗口所属应用名称，返回小写字串；失败返回 ''。"""
    try:
        proc = subprocess.run(
            ['xprop', '-id', win_id, 'WM_CLASS'],
            capture_output=True, text=True, timeout=2,
        )
        if proc.returncode != 0:
            return ""
        # 输出: WM_CLASS(STRING) = "firefox", "Firefox"
        line = proc.stdout.strip()
        if '=' not in line:
            return ""
        val = line.split('=', 1)[1].strip()
        # 取第一个引号内的值（实例名，小写）
        if val.startswith('"'):
            name = val.split('"')[1]
            return name.lower()
        return ""
    except Exception:
        return ""


def activate_window(win_id):
    """使用 xdotool 激活窗口（不影响本工具自身的窗口）"""
    cmd = ['xdotool', 'windowactivate', win_id]
    log.info("Running: %s", " ".join(cmd))
    proc = subprocess.run(cmd, capture_output=True, text=True)
    stdout = (proc.stdout or "").strip()
    stderr = (proc.stderr or "").strip()
    if stdout:
        log.info("  xdotool stdout: %s", stdout)
    if proc.returncode != 0:
        log.error("  xdotool exited %d. stderr: %s", proc.returncode, stderr or "<empty>")
    elif stderr:
        log.warning("  xdotool stderr (non-fatal): %s", stderr)
    else:
        log.info("  window %s activated.", win_id)


def get_active_window_id():
    """返回当前活动（聚焦）窗口的 id，规范化为整数；失败返回 None。

    `xdotool getactivewindow` 输出的是十进制窗口 id，而 `wmctrl -l` 给出的是
    形如 0x03a00003 的十六进制字符串。为便于比较，这里统一返回十进制整数，
    调用方再用 int(wmctrl_id, 16) 做匹配。
    """
    cmd = ['xdotool', 'getactivewindow']
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=2)
    except Exception as exc:
        log.warning("  getactivewindow failed: %s", exc)
        return None
    if proc.returncode != 0:
        log.debug("  getactivewindow exit=%d (no active window?)", proc.returncode)
        return None
    out = (proc.stdout or "").strip()
    try:
        return int(out)
    except ValueError:
        log.debug("  getactivewindow gave unparsable id: %r", out)
        return None


def normalize_win_id(win_id):
    """把 wmctrl 的十六进制窗口 id（如 0x03a00003）转换为十进制整数，失败返回 None。"""
    try:
        return int(win_id, 16)
    except (ValueError, TypeError):
        return None


def ocr_image(img, win_id=""):
    """对截图做 OCR（tesseract CLI），返回识别出的文本；任何失败都返回 ""。

    NOTE: 这是阻塞调用 (blocking)，会在线程池中并发执行（在截图成功之后）。
    通过 stdin 把 PNG 喂给 tesseract，从 stdout 读取文本，避免临时文件。
    语言由 OCR_LANG 指定（chi_sim+eng）。
    """
    if not (OCR_ENABLED and _OCR_AVAILABLE):
        return ""
    t0 = time.perf_counter()
    try:
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        png_bytes = buf.getvalue()
    except Exception as exc:
        log.warning("  OCR skip %s: cannot encode image: %s", win_id, exc)
        return ""

    cmd = ['tesseract', 'stdin', 'stdout', '-l', OCR_LANG]
    try:
        proc = subprocess.run(cmd, input=png_bytes, capture_output=True)
    except FileNotFoundError:
        log.warning("  OCR unavailable: 'tesseract' not found at OCR time.")
        return ""

    elapsed = (time.perf_counter() - t0) * 1000
    if proc.returncode != 0:
        stderr = proc.stderr.decode('utf-8', 'replace').strip()
        log.warning("  OCR FAILED for %s after %.0f ms | exit=%d | stderr: %s",
                    win_id, elapsed, proc.returncode, stderr or "<empty>")
        return ""

    text = proc.stdout.decode('utf-8', 'replace')
    collapsed = " ".join(text.split())   # 折叠空白/换行，便于子串搜索
    log.info("  OCR %s in %.0f ms -> %d chars", win_id, elapsed, len(collapsed))
    return collapsed


# ---------------------------- 并发截图 / Concurrent capture ----------------------------
async def capture_stream(windows, executor, on_result, on_ocr=None):
    """并发地为所有窗口截图，每张完成后立即回调 on_result(win_id, title, img)。

    使用 loop.run_in_executor 把阻塞的 capture_window 调度到线程池；
    总耗时约等于最慢的那一张，而不是所有截图耗时之和。逐张回调（而非
    全部 gather 后统一返回）可让 UI 增量更新、保持响应。

    截图成功后，再异步地对该图做 OCR（同样在线程池），完成后回调
    on_ocr(win_id, text)，把识别文本纳入搜索。OCR 不阻塞图像的显示。
    """
    loop = asyncio.get_event_loop()
    log.info("Scheduling %d capture task(s) onto the executor ...", len(windows))

    async def one(win_id, title):
        img = await loop.run_in_executor(executor, capture_window, win_id, title)
        on_result(win_id, title, img)
        if img is not None and on_ocr is not None and OCR_ENABLED and _OCR_AVAILABLE:
            text = await loop.run_in_executor(executor, ocr_image, img, win_id)
            on_ocr(win_id, text)

    await asyncio.gather(*(one(win_id, title) for win_id, title in windows))


# ---------------------------- 外观配置 / Appearance config ----------------------------
# 主题类型："dark" 或 "light"，在此切换。
THEME = "dark"

# 全局字体设置。
# 注意：tkinter 在 Linux 上默认使用 X core 位图字体（无抗锯齿），
# 必须显式指定 TrueType 字族才能获得平滑渲染。
# 推荐安装 Noto Sans CJK（中日韩 + 拉丁文均表现优秀）：
#   sudo apt install fonts-noto-cjk
FONT_FAMILY = "Noto Sans CJK SC"   # 例如 "Noto Sans CJK SC"、"WenQuanYi Micro Hei"、"DejaVu Sans"
FONT_SIZE = 13                     # 基础字号（放大后的默认大小）
TITLE_FONT_SIZE = 14               # 放大预览顶部标题栏的字号

# 各主题的调色板。可自由增删颜色或新增主题。
THEMES = {
    "dark": {
        "bg":          "#1e1e1e",   # 窗口/框架背景
        "fg":          "#e0e0e0",   # 普通文字
        "canvas_bg":   "#1e1e1e",   # 滚动画布背景
        "tile_bg":     "#2a2a2a",   # 预览磁贴背景
        "entry_bg":    "#2d2d2d",   # 搜索框背景
        "entry_fg":    "#ffffff",
        "select_bg":   "#264f78",   # 按钮悬停/选中
        "thumb_pad":   "#3a3a3a",   # 缩略图留白/失败占位背景
        "tip_bg":      "#000000",   # 放大预览图片背景
        "tip_border":  "#555555",
        "tiptitle_bg": "#111111",   # 放大预览标题栏背景
        "tiptitle_fg": "#ffffff",
    },
    "light": {
        "bg":          "#f0f0f0",
        "fg":          "#202020",
        "canvas_bg":   "#f0f0f0",
        "tile_bg":     "#ffffff",
        "entry_bg":    "#ffffff",
        "entry_fg":    "#000000",
        "select_bg":   "#cce4ff",
        "thumb_pad":   "#ffffff",
        "tip_bg":      "#000000",
        "tip_border":  "#888888",
        "tiptitle_bg": "#222222",
        "tiptitle_fg": "#ffffff",
    },
}


# ---------------------------- 主 GUI 应用 / Main GUI ----------------------------
# 悬停放大预览的可调参数
HOVER_DELAY_S = 0.3            # 鼠标需在缩略图上静止多少秒后才弹出放大预览
HOVER_MOVE_THRESHOLD_PX = 4   # 超过该像素位移即视为“移动”（隐藏预览并重置计时）
HOVER_POLL_MS = 80            # 指针轮询间隔（毫秒）

# 窗口与缩略图尺寸参数
WINDOW_W = 1600              # 主窗口宽度
WINDOW_H = 900               # 主窗口高度
THUMB_W = 360                # 缩略图宽（2x）
THUMB_H = 240                # 缩略图高（2x）
MAX_COLS = 4                 # 每行预览数
LARGE_CAP_RATIO = 0.9        # 放大预览图最大不超过屏幕的该比例
REFRESH_INTERVAL_S = 5       # 周期刷新间隔（秒）= 描述中的 X：每隔 X 秒做一拍轻量刷新
REFRESH_BATCH_SIZE = 3       # 每拍额外更新的后台窗口数 = 描述中的 Y（聚焦窗口总是额外更新）

# ---- OCR 配置 ----
OCR_ENABLED = False           # 截图更新后是否对其做 OCR，并把识别文本纳入搜索
OCR_LANG = "chi_sim+eng"     # tesseract 语言（需安装 tesseract-ocr-chi-sim / -eng 语言包）


class WindowPreviewApp(tk.Tk):
    def __init__(self):
        super().__init__()
        log.info("Initializing GUI ...")
        self.title("窗口预览工具")
        self.geometry(f"{WINDOW_W}x{WINDOW_H}")

        # 当前主题调色板
        self.theme = THEMES.get(THEME, THEMES["dark"])

        # 应用全局字体与主题（必须在创建其它控件之前）
        self._setup_fonts()
        self._apply_theme()

        # 搜索框专用字体（比基础字号大 6 号，更易辨识）
        self._search_font = tkfont.Font(
            family=(FONT_FAMILY or tkfont.nametofont("TkDefaultFont").cget("family")),
            size=FONT_SIZE + 6,
        )

        # 存储当前所有窗口预览信息
        self.window_items = []   # 每项: {'frame','win_id','title','img_label','title_label','large_img','preview_photo'}
        self.items_by_id = {}    # win_id -> item，便于增量更新时快速查找

        # 线程池，用于并发执行阻塞的截图命令
        self.executor = ThreadPoolExecutor(
            max_workers=min(8, (os.cpu_count() or 4)),
            thread_name_prefix="capture",
        )

        # 状态标志
        self._is_refreshing = False   # 防止并发刷新
        self._has_focus = True        # 跟踪焦点状态（仅用于日志/点击行为）
        self._tick_after = None       # 周期刷新（每 X 秒一拍）的 after id

        # 省电刷新：后台窗口 id 的待更新队列（已打乱）。每拍弹出至多 Y 个；
        # 队列耗尽时重新收集所有后台窗口 id 并再次打乱。
        self._bg_queue = []

        # 悬停放大预览 (hover preview) 状态
        self._preview_tip = None      # 当前弹出的 Toplevel
        self._tip_item = None         # 当前预览对应的 window_item
        self._hover_target = None     # 指针当前悬停的 window_item（尚未/已显示）
        self._still_since = 0.0       # 指针在当前位置开始静止的时间戳 (perf_counter)
        self._last_pointer = (-1, -1) # 上一次轮询时的指针位置
        self._hover_poll_after = None # 轮询循环的 after id

        # 顶部控制栏
        control_frame = ttk.Frame(self)
        control_frame.pack(fill=tk.X, padx=5, pady=5)

        ttk.Label(control_frame, text="搜索:").pack(side=tk.LEFT, padx=5)
        self.search_var = tk.StringVar()
        self.search_entry = ttk.Entry(control_frame, textvariable=self.search_var,
                                       font=self._search_font)
        self.search_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=5, ipady=8)
        self.search_entry.bind("<KeyRelease>", self.on_search)
        self.search_entry.bind("<Control-a>", self.on_select_all)
        self.search_entry.bind("<Control-A>", self.on_select_all)

        # 全局键盘绑定：任何按键输入时，聚焦搜索框并隐藏鼠标悬停预览
        self.bind_all("<Key>", self.on_global_key)

        self.refresh_btn = ttk.Button(control_frame, text="刷新",
                                       command=lambda: self.refresh(force=True))
        self.refresh_btn.pack(side=tk.RIGHT, padx=5)

        self.status_var = tk.StringVar(value="就绪")
        self.status_label = ttk.Label(control_frame, textvariable=self.status_var)
        self.status_label.pack(side=tk.RIGHT, padx=10)

        # 滚动区域
        self.canvas = tk.Canvas(self, bg=self.theme["canvas_bg"], highlightthickness=0)
        self.scrollbar = ttk.Scrollbar(self, orient=tk.VERTICAL, command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.scrollbar.set)

        self.scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        # 内部容器 Frame (放在 Canvas 中)
        self.inner_frame = ttk.Frame(self.canvas)
        self.canvas.create_window((0, 0), window=self.inner_frame, anchor='nw')

        # 绑定事件：当内部 Frame 尺寸改变时，更新 Canvas 滚动区域
        self.inner_frame.bind("<Configure>", self.on_frame_configure)

        # 鼠标滚轮滚动。X11 (Kubuntu) 上滚轮是 Button-4/5；
        # Windows/macOS 则是 <MouseWheel>。两种都绑定，确保通用。
        # 绑定到整个应用 (bind_all)，这样鼠标在任意子控件上滚动都有效。
        self.bind_all("<Button-4>", self.on_mousewheel)
        self.bind_all("<Button-5>", self.on_mousewheel)
        self.bind_all("<MouseWheel>", self.on_mousewheel)

        # 监听整个应用的“激活/失活”，而不是 <FocusIn>/<FocusOut>。
        # <FocusIn>/<FocusOut> 是按控件触发的：点击搜索框会让顶层窗口收到
        # FocusOut（焦点移到子控件），从而被误判为“失焦”。而 <Activate>/
        # <Deactivate> 在窗口管理器层面触发，只关心整个窗口是否为活动窗口，
        # 子控件之间移动焦点不会触发它们。
        self.bind("<Activate>", self.on_activate)
        self.bind("<Deactivate>", self.on_deactivate)

        # 确保退出时清理线程池
        self.protocol("WM_DELETE_WINDOW", self.on_close)

        # 初次加载（立即做一拍，填充焦点窗口 + 首批后台窗口）
        log.info("Scheduling initial refresh ...")
        self.after(100, lambda: self.refresh(force=True))   # 等待 GUI 完全启动

        # 启动周期刷新循环（每 X 秒一拍，始终运行；每拍只做少量截图，省电）
        self._tick_after = self.after(int(REFRESH_INTERVAL_S * 1000), self._tick)

        # 启动悬停轮询循环
        self._hover_poll_after = self.after(HOVER_POLL_MS, self._poll_hover)

    # ------------------------ 外观 / appearance ------------------------
    def _setup_fonts(self):
        """放大全局命名字体；这会同时影响 tk 与 ttk 控件。"""
        for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont",
                     "TkHeadingFont", "TkFixedFont"):
            try:
                f = tkfont.nametofont(name)
            except tk.TclError:
                continue
            f.configure(size=FONT_SIZE)
            if FONT_FAMILY:
                f.configure(family=FONT_FAMILY)
        # 供放大预览标题栏使用的字体对象
        self.title_font = tkfont.Font(
            family=(FONT_FAMILY or tkfont.nametofont("TkDefaultFont").cget("family")),
            size=TITLE_FONT_SIZE, weight="bold",
        )
        log.info("Fonts configured: family=%r size=%d", FONT_FAMILY or "<default>", FONT_SIZE)

    def _apply_theme(self):
        """应用主题调色板到根窗口与所有 ttk 控件。"""
        c = self.theme
        self.configure(bg=c["bg"])

        style = ttk.Style(self)
        # 'clam' 主题最易于自定义颜色
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        style.configure(".",
                        background=c["bg"], foreground=c["fg"],
                        fieldbackground=c["entry_bg"])
        style.configure("TFrame", background=c["bg"])
        style.configure("Tile.TFrame", background=c["tile_bg"])
        style.configure("TLabel", background=c["bg"], foreground=c["fg"])
        style.configure("Tile.TLabel", background=c["tile_bg"], foreground=c["fg"])
        style.configure("TButton", background=c["tile_bg"], foreground=c["fg"])
        style.map("TButton",
                  background=[("active", c["select_bg"]), ("disabled", c["bg"])],
                  foreground=[("disabled", "#777777")])
        style.configure("TEntry",
                        fieldbackground=c["entry_bg"], foreground=c["entry_fg"],
                        insertcolor=c["fg"])
        style.configure("TScrollbar",
                        background=c["tile_bg"], troughcolor=c["bg"],
                        arrowcolor=c["fg"])
        log.info("Theme applied: %s", THEME)

    # ------------------------ 事件回调 / callbacks ------------------------
    def on_frame_configure(self, event):
        """更新 Canvas 滚动区域以匹配内部 Frame 大小"""
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def on_mousewheel(self, event):
        """鼠标滚轮滚动 Canvas。

        X11 (Kubuntu): event.num == 4 (上) / 5 (下)。
        Windows/macOS: 使用 event.delta（正数上滚，负数下滚）。
        """
        if event.num == 4:
            self.canvas.yview_scroll(-1, "units")
        elif event.num == 5:
            self.canvas.yview_scroll(1, "units")
        elif event.delta:
            self.canvas.yview_scroll(-1 if event.delta > 0 else 1, "units")

    # ------------------------ 悬停放大预览 / hover preview ------------------------
    # 工作方式：以 HOVER_POLL_MS 周期轮询指针位置。
    #   - 指针位移超过阈值 -> 视为“移动”：立即隐藏已显示的预览，并重置静止计时；
    #     同时记录指针下方的缩略图作为新的悬停目标。
    #   - 指针保持静止（位移不超过阈值）达到 HOVER_DELAY_S 秒，且正悬停在某个
    #     缩略图上 -> 弹出该窗口的放大预览。
    # 这种轮询方式天然规避了“预览盖住指针导致 Enter/Leave 抖动”的问题。
    def _get_preview_photo(self, item):
        """惰性生成并缓存放大预览用的 PhotoImage（large_img 已限制在屏幕尺寸内）。"""
        if item.get('preview_photo') is None:
            item['preview_photo'] = ImageTk.PhotoImage(item['large_img'])
        return item['preview_photo']

    def _item_under_pointer(self, px, py):
        """返回指针 (px,py 为 root 坐标) 正下方缩略图对应的 window_item，否则 None。"""
        try:
            widget = self.winfo_containing(px, py)
        except KeyError:
            widget = None
        if widget is None:
            return None
        for item in self.window_items:
            if widget is item['img_label']:
                return item
        return None

    def _poll_hover(self):
        """周期性轮询指针：决定何时弹出 / 隐藏放大预览。"""
        try:
            px, py = self.winfo_pointerx(), self.winfo_pointery()
            lx, ly = self._last_pointer
            moved = (abs(px - lx) > HOVER_MOVE_THRESHOLD_PX or
                     abs(py - ly) > HOVER_MOVE_THRESHOLD_PX)
            self._last_pointer = (px, py)

            if moved:
                # 任何明显移动都隐藏当前预览，并重置静止计时
                if self._preview_tip is not None:
                    self._destroy_tip()
                self._hover_target = self._item_under_pointer(px, py)
                self._still_since = time.perf_counter()
            else:
                # 指针静止：若悬停在缩略图上且尚未显示，计时到点就弹出
                if (self._hover_target is not None
                        and self._preview_tip is None
                        and self._hover_target.get('large_img') is not None):
                    if time.perf_counter() - self._still_since >= HOVER_DELAY_S:
                        self._show_preview(self._hover_target, px, py)
        finally:
            # 始终重新安排下一次轮询
            self._hover_poll_after = self.after(HOVER_POLL_MS, self._poll_hover)

    def _show_preview(self, item, px, py):
        """在指针 (px,py) 附近弹出 item 的放大预览。"""
        self._destroy_tip()

        photo = self._get_preview_photo(item)
        pw, ph = photo.width(), photo.height()
        log.info("Hover preview for %s (%dx%d) after %.1fs still.",
                 item['win_id'], pw, ph, HOVER_DELAY_S)

        tip = tk.Toplevel(self)
        tip.wm_overrideredirect(True)   # 无边框
        tip.configure(bg=self.theme["tip_border"])
        try:
            tip.attributes('-topmost', True)
        except tk.TclError:
            pass

        # 顶部标题栏（应用名 + 窗口标题，置于截图之上）
        title_text = item['title']
        if item.get('app_name'):
            title_text = f"[{item['app_name']}] {title_text}"
        title_lbl = tk.Label(
            tip, text=title_text, anchor='w', justify='left',
            bg=self.theme["tiptitle_bg"], fg=self.theme["tiptitle_fg"],
            font=self.title_font, padx=8, pady=4, wraplength=pw,
        )
        title_lbl.pack(fill=tk.X, padx=1, pady=(1, 0))

        # 窗口截图
        lbl = tk.Label(tip, image=photo, bg=self.theme["tip_bg"],
                       borderwidth=0)
        lbl.image = photo   # 保持引用
        lbl.pack(padx=1, pady=(0, 1))

        # 用实际请求尺寸（含标题栏高度）来定位，保证不超出屏幕。
        tip.update_idletasks()
        tw = tip.winfo_reqwidth()
        th = tip.winfo_reqheight()

        # 定位：尽量放在指针右侧；放不下就放左侧；再不行就水平居中。
        sw = self.winfo_screenwidth()
        sh = self.winfo_screenheight()
        x = px + 24
        if x + tw > sw:
            x = px - tw - 24        # 改放指针左侧
        if x < 0:
            x = max(0, (sw - tw) // 2)         # 实在放不下就水平居中
        y = py - th // 2            # 垂直方向以指针为中心
        if y + th > sh:
            y = sh - th
        if y < 0:
            y = 0
        tip.geometry("+%d+%d" % (int(x), int(y)))

        self._preview_tip = tip
        self._tip_item = item

    def _destroy_tip(self):
        if self._preview_tip is not None:
            self._preview_tip.destroy()
            self._preview_tip = None
            self._tip_item = None

    def on_deactivate(self, event):
        """整个应用窗口变为非活动（用户切到别的应用）-> 记录失焦状态。

        省电刷新循环始终运行（每 X 秒一拍），不再依赖聚焦状态来开关，
        因此这里只更新标志位用于日志/点击行为。
        """
        if event.widget is not self:
            return
        if not self._has_focus:
            return  # 已经处于失活状态
        self._has_focus = False
        log.info("App deactivated (lost focus).")

    def on_activate(self, event):
        """整个应用窗口变为活动 -> 记录聚焦状态（不影响周期刷新循环）。"""
        if event.widget is not self:
            return
        if self._has_focus:
            return  # 已经处于活动状态
        self._has_focus = True
        log.info("App activated (gained focus).")

    # ------------------------ 周期刷新 / periodic tick ------------------------
    def _tick(self):
        """周期刷新的一拍：始终运行，做一次轻量刷新（焦点窗口 + Y 个后台窗口），再重新排期。"""
        self._tick_after = None
        log.info("Periodic refresh tick.")
        self.refresh()
        self._tick_after = self.after(int(REFRESH_INTERVAL_S * 1000), self._tick)

    def on_click_preview(self, win_id):
        """点击预览：仅激活目标窗口，本工具窗口保持原样（不隐藏、不关闭）。"""
        log.info("Preview clicked -> activating window %s (this tool stays put).", win_id)
        activate_window(win_id)
        # 不再 withdraw()。激活其它窗口会使本工具失活（<Deactivate>），
        # on_deactivate 会把 _has_focus 置为 False 并启动后台刷新；
        # 当用户切回本工具时 <Activate> 会停止后台刷新（聚焦期间不刷新）。

    def on_search(self, event):
        """搜索框输入时过滤预览：隐藏不匹配项，并把匹配项紧凑重排。"""
        shown = self._relayout()
        log.debug("Search filter %r -> %d/%d shown",
                  self.search_var.get().strip().lower(), shown, len(self.window_items))

    def on_select_all(self, event):
        """Ctrl+A: 全选搜索框文本。"""
        self.search_entry.selection_range(0, tk.END)
        return "break"

    def on_global_key(self, event):
        """全局按键：聚焦搜索框，隐藏悬停预览；如为可打印字符则直接插入搜索框。

        这样当用户开始打字时，搜索框自动获得焦点、预览消失，无需手动点击搜索框。
        """
        # 始终隐藏悬停预览
        if self._preview_tip is not None:
            self._destroy_tip()
        self._hover_target = None
        self._still_since = 0.0

        # 如果搜索框已获焦点，无需干预
        if event.widget is self.search_entry:
            return

        # 仅将可打印字符及空格重定向到搜索框（忽略功能键、修饰键等）
        if event.char and len(event.char) == 1 and (event.char.isprintable() or event.char == ' '):
            self.search_entry.focus_set()
            self.search_entry.insert(tk.INSERT, event.char)
            self.on_search(event)
            return "break"

    def on_close(self):
        log.info("Shutting down: cleaning up executor ...")
        if self._tick_after is not None:
            self.after_cancel(self._tick_after)
            self._tick_after = None
        if self._hover_poll_after is not None:
            self.after_cancel(self._hover_poll_after)
            self._hover_poll_after = None
        self._destroy_tip()
        self.executor.shutdown(wait=False, cancel_futures=True)
        self.destroy()

    # ------------------------ 刷新流程 / refresh ------------------------
    def refresh(self, force=False):
        """省电刷新：每拍只更新「当前聚焦窗口 + 至多 Y 个后台窗口」，而非全量截图。

        步骤：
          1) 用 wmctrl 取窗口列表并调和磁贴（增/删/重排）——这步很轻量。
          2) 确定当前聚焦/活动窗口；它每拍都更新。
          3) 从「已打乱的后台队列」里取至多 REFRESH_BATCH_SIZE 个 id（跳过已消失
             的）；队列耗尽时收集所有当前后台 id 重新打乱再填充。
          4) 仅对这一小批窗口并发截图。

        force 仅保留给手动刷新/首次加载使用；省电策略下它不再改变行为。
        """
        if self._is_refreshing:
            log.info("Refresh already in progress -> ignoring this request.")
            return

        self._is_refreshing = True
        self.refresh_btn.config(state=tk.DISABLED)
        self.status_var.set("刷新中…")
        log.info("=== Refresh START ===")
        t0 = time.perf_counter()

        # 1) 获取当前窗口列表（快速命令，主线程执行），并调和磁贴。
        windows = get_window_list()
        self._reconcile(windows)

        if not windows:
            log.info("No windows found.")
            self._finish_refresh(t0)
            return

        title_by_id = {wid: title for wid, title in windows}
        current_ids = set(title_by_id)

        # 2) 确定当前聚焦窗口（每拍都更新它）。
        active_int = get_active_window_id()
        focused_id = None
        if active_int is not None:
            for wid in current_ids:
                if normalize_win_id(wid) == active_int:
                    focused_id = wid
                    break
        log.info("Focused window this tick: %s", focused_id or "<none/our own>")

        # 3) 组装本拍的截图目标：聚焦窗口 + 一批轮转的后台窗口。
        targets = []
        seen = set()
        if focused_id is not None:
            targets.append(focused_id)
            seen.add(focused_id)

        for wid in self._select_background_batch(current_ids, focused_id):
            if wid not in seen:
                targets.append(wid)
                seen.add(wid)

        capture_list = [(wid, title_by_id[wid]) for wid in targets]
        log.info("Power-efficient tick: capturing %d/%d window(s) "
                 "(focused + up to %d background).",
                 len(capture_list), len(current_ids), REFRESH_BATCH_SIZE)

        if not capture_list:
            # 没有可截图的目标（极少见：列表里只有已消失的 id）。
            self._finish_refresh(t0)
            return

        # 4) 后台线程并发截图；每张完成后调度回主线程增量更新对应磁贴。
        def worker():
            log.info("Capture worker thread started (asyncio).")

            def on_result(win_id, title, img):
                self.after(0, lambda wid=win_id, t=title, im=img:
                           self._apply_capture(wid, t, im))

            def on_ocr(win_id, text):
                self.after(0, lambda wid=win_id, tx=text:
                           self._apply_ocr(wid, tx))

            try:
                asyncio.run(capture_stream(capture_list, self.executor, on_result, on_ocr))
            except Exception as exc:
                log.exception("Capture worker crashed: %s", exc)
            self.after(0, lambda: self._finish_refresh(t0))

        threading.Thread(target=worker, name="capture-loop", daemon=True).start()

    def _select_background_batch(self, current_ids, focused_id):
        """从已打乱的后台队列里取出至多 REFRESH_BATCH_SIZE 个仍存在的后台窗口 id。

        - 队列里跳过：已消失的 id、以及当前聚焦窗口（聚焦窗口已单独更新）。
        - 队列耗尽时：收集所有当前后台 id，打乱，重新填充（描述中的「running out
          时收集所有 id 再次 shuffle」）。
        - 单拍内绝不重复同一窗口；故批量上限不超过当前后台窗口总数。
        """
        pool_size = sum(1 for wid in current_ids if wid != focused_id)
        if pool_size == 0:
            return []

        limit = min(REFRESH_BATCH_SIZE, pool_size)
        batch = []
        while len(batch) < limit:
            if not self._bg_queue:
                refill = [wid for wid in current_ids if wid != focused_id]
                random.shuffle(refill)
                self._bg_queue = refill
                log.info("Background queue exhausted -> reshuffled %d id(s).", len(refill))
            wid = self._bg_queue.pop(0)
            if wid not in current_ids or wid == focused_id:
                continue   # 跳过已消失 / 此刻恰为聚焦窗口的 id
            if wid in batch:
                continue   # 重新打乱后可能再次遇到本拍已选的 id
            batch.append(wid)
        return batch

    # ------------------------ 增量构建 / incremental build ------------------------
    def _cap_large(self, img):
        """把截图限制在屏幕尺寸内（只缩小、绝不放大，文字保持清晰）。"""
        cap_w = int(self.winfo_screenwidth() * LARGE_CAP_RATIO)
        cap_h = int(self.winfo_screenheight() * LARGE_CAP_RATIO)
        if img.width > cap_w or img.height > cap_h:
            img = img.copy()
            img.thumbnail((cap_w, cap_h), Image.LANCZOS)
        return img

    def _make_thumb_photo(self, large):
        """由大图（或 None 占位）生成固定尺寸缩略图的 PhotoImage。"""
        thumb = Image.new('RGB', (THUMB_W, THUMB_H), color=self.theme["thumb_pad"])
        if large is not None:
            t = large.copy()
            t.thumbnail((THUMB_W, THUMB_H), Image.LANCZOS)
            thumb.paste(t, ((THUMB_W - t.width) // 2, (THUMB_H - t.height) // 2))
        return ImageTk.PhotoImage(thumb)

    def _create_tile(self, win_id, title):
        """为一个新窗口创建预览磁贴（初始为占位图，截图到达后再填充）。"""
        preview_frame = ttk.Frame(self.inner_frame, style="Tile.TFrame",
                                  relief=tk.RAISED, borderwidth=1)

        photo = self._make_thumb_photo(None)
        img_label = ttk.Label(preview_frame, image=photo, style="Tile.TLabel")
        img_label.image = photo
        img_label.pack()

        app_name = get_app_name(win_id)
        if app_name:
            app_label = ttk.Label(preview_frame, text=app_name,
                                  style="Tile.TLabel", wraplength=THUMB_W)
            app_label.pack()
        else:
            app_label = None

        disp_title = (title[:30] + '...') if len(title) > 30 else title
        title_label = ttk.Label(preview_frame, text=disp_title,
                                style="Tile.TLabel", wraplength=THUMB_W)
        title_label.pack()

        for w in (preview_frame, img_label, title_label):
            w.bind("<Button-1>", lambda e, wid=win_id: self.on_click_preview(wid))
        if app_label is not None:
            app_label.bind("<Button-1>", lambda e, wid=win_id: self.on_click_preview(wid))

        item = {
            'frame': preview_frame,
            'win_id': win_id,
            'title': title,
            'app_name': app_name,
            'img_label': img_label,
            'app_label': app_label,
            'title_label': title_label,
            'large_img': None,      # 最近一次成功截图（PIL），失败时保留旧值
            'preview_photo': None,  # 惰性生成并缓存的放大 PhotoImage
            'ocr': '',              # 最近一次 OCR 文本（小写，纳入搜索）
        }
        self.window_items.append(item)
        self.items_by_id[win_id] = item
        return item

    def _reconcile(self, windows):
        """根据最新窗口列表调和磁贴：移除消失的、新增新出现的、更新标题、重排布局。"""
        self._destroy_tip()
        current_ids = [wid for wid, _ in windows]
        current_set = set(current_ids)

        # 1) 移除已不可访问（不在列表中）的窗口磁贴
        removed = 0
        for item in list(self.window_items):
            if item['win_id'] not in current_set:
                if self._hover_target is item:
                    self._hover_target = None
                item['frame'].destroy()
                self.window_items.remove(item)
                self.items_by_id.pop(item['win_id'], None)
                removed += 1

        # 2) 新增新窗口；已有窗口仅在标题变化时更新
        added = 0
        for win_id, title in windows:
            item = self.items_by_id.get(win_id)
            if item is None:
                self._create_tile(win_id, title)
                added += 1
            elif item['title'] != title:
                item['title'] = title
                disp = (title[:30] + '...') if len(title) > 30 else title
                item['title_label'].configure(text=disp)

        # 3) 按 wmctrl 顺序重排
        order = {wid: i for i, wid in enumerate(current_ids)}
        self.window_items.sort(key=lambda it: order.get(it['win_id'], 1 << 30))

        log.info("Reconcile: +%d new, -%d gone, %d total.",
                 added, removed, len(self.window_items))

        # 4) 重新布局（紧凑排列匹配项，复用当前搜索过滤）
        self._relayout()

    def _relayout(self):
        """按当前搜索过滤重新布局：只显示匹配项，并让它们连续紧凑排列。"""
        # 按空格分词，使用 'and' 运算：每个关键词都必须出现在标题中。
        keywords = self.search_var.get().strip().lower().split()
        idx = 0
        for item in self.window_items:
            # 搜索范围：应用名 + 标题 + OCR 文本（若该窗口已有 OCR 结果）。
            haystack = item['title'].lower()
            if item.get('app_name'):
                haystack = item['app_name'] + " " + haystack
            if item.get('ocr'):
                haystack = haystack + " " + item['ocr']
            if all(kw in haystack for kw in keywords):
                r, c = divmod(idx, MAX_COLS)
                item['frame'].grid(row=r, column=c, padx=5, pady=5, sticky='nsew')
                idx += 1
            else:
                item['frame'].grid_remove()
        for c in range(MAX_COLS):
            self.inner_frame.columnconfigure(c, weight=1)
        self.inner_frame.update_idletasks()
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        return idx   # 匹配（显示）数量

    def _apply_capture(self, win_id, title, img):
        """一张截图完成后在主线程更新对应磁贴。

        - 成功：更新大图与缩略图。
        - 失败 (img is None)：保留旧截图（todo 第 2 条）；若从未有过截图则保持占位。
        """
        item = self.items_by_id.get(win_id)
        if item is None:
            return  # 该窗口在本轮刷新中已消失

        if item['title'] != title:
            item['title'] = title
            disp = (title[:30] + '...') if len(title) > 30 else title
            item['title_label'].configure(text=disp)

        if img is None:
            if item['large_img'] is None:
                log.debug("No capture yet for %s; keeping placeholder.", win_id)
            else:
                log.debug("Capture unavailable for %s; keeping previous image.", win_id)
            return

        large = self._cap_large(img)
        item['large_img'] = large
        item['preview_photo'] = None   # 失效放大预览缓存
        photo = self._make_thumb_photo(large)
        item['img_label'].configure(image=photo)
        item['img_label'].image = photo   # 保持引用

    def _apply_ocr(self, win_id, text):
        """OCR 完成后在主线程把识别文本存入磁贴，并按当前搜索重排。

        若已有搜索关键词，新到的 OCR 文本可能让该窗口变为匹配/不匹配，
        故重新布局一次。空结果不覆盖旧文本（截图可能暂时不可读）。
        """
        item = self.items_by_id.get(win_id)
        if item is None:
            return  # 该窗口在本轮刷新中已消失
        if not text:
            return
        item['ocr'] = text.lower()
        if self.search_var.get().strip():
            self._relayout()

    def _finish_refresh(self, t0):
        elapsed = (time.perf_counter() - t0) * 1000
        self._is_refreshing = False
        self.refresh_btn.config(state=tk.NORMAL)
        self.status_var.set("就绪 (%d 个窗口, %.0f ms)" % (len(self.window_items), elapsed))
        log.info("=== Refresh DONE in %.0f ms (%d windows) ===", elapsed, len(self.window_items))


# ---------------------------- 主程序入口 / entry point ----------------------------
if __name__ == "__main__":
    log.info("Starting window preview tool.")
    if not check_dependencies():
        log.error("Exiting due to missing dependencies.")
        sys.exit(1)

    app = WindowPreviewApp()
    try:
        app.mainloop()
    finally:
        log.info("Main loop exited; shutting down executor.")
        app.executor.shutdown(wait=False)
