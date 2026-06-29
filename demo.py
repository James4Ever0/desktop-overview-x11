#!/usr/bin/env python3
"""
Quick & Dirty Window Preview Tool for Kubuntu 22.04 (X11)
Dependencies: wmctrl, import (ImageMagick), xdotool, python3-tk, Pillow
"""

import os
import sys
import subprocess
import io
import tkinter as tk
from tkinter import ttk, messagebox
from PIL import Image, ImageTk

# ---------------------------- 依赖检查 ----------------------------
def check_dependencies():
    missing = []
    for cmd in ['wmctrl', 'import', 'xdotool']:
        if subprocess.call(['which', cmd], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) != 0:
            missing.append(cmd)
    if missing:
        msg = "缺少以下依赖: " + ", ".join(missing) + "\n请安装:\n"
        if 'wmctrl' in missing:
            msg += "  sudo apt install wmctrl\n"
        if 'import' in missing:
            msg += "  sudo apt install imagemagick\n"
        if 'xdotool' in missing:
            msg += "  sudo apt install xdotool\n"
        messagebox.showerror("依赖缺失", msg)
        return False
    return True

# ---------------------------- 窗口操作函数 ----------------------------
def get_window_list():
    """通过 wmctrl -l 获取窗口列表，返回 [(id, title), ...]"""
    try:
        output = subprocess.check_output(['wmctrl', '-l'], text=True)
    except subprocess.CalledProcessError:
        return []
    windows = []
    for line in output.strip().split('\n'):
        if not line:
            continue
        parts = line.strip().split(maxsplit=3)
        if len(parts) >= 4:
            win_id = parts[0]          # 如 0x03a00003
            title = parts[3]           # 窗口标题
            windows.append((win_id, title))
    return windows

def capture_window(win_id):
    """使用 import -window 截图，返回 PIL Image 对象，失败返回 None"""
    try:
        proc = subprocess.run(
            ['import', '-window', win_id, 'png:-'],
            capture_output=True, check=True
        )
        img = Image.open(io.BytesIO(proc.stdout))
        return img
    except Exception:
        return None

def activate_window(win_id):
    """使用 xdotool 激活窗口"""
    try:
        subprocess.Popen(['xdotool', 'windowactivate', win_id])
    except Exception:
        pass

# ---------------------------- 主 GUI 应用 ----------------------------
class WindowPreviewApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("窗口预览工具")
        self.geometry("900x600")

        # 存储当前所有窗口预览信息
        self.window_items = []   # 每个元素: {'frame': frame, 'win_id': id, 'title': str, 'img_label': label}

        # 顶部控制栏
        control_frame = ttk.Frame(self)
        control_frame.pack(fill=tk.X, padx=5, pady=5)

        ttk.Label(control_frame, text="搜索:").pack(side=tk.LEFT, padx=5)
        self.search_var = tk.StringVar()
        self.search_entry = ttk.Entry(control_frame, textvariable=self.search_var)
        self.search_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=5)
        self.search_entry.bind("<KeyRelease>", self.on_search)

        self.refresh_btn = ttk.Button(control_frame, text="刷新", command=self.refresh)
        self.refresh_btn.pack(side=tk.RIGHT, padx=5)

        # 滚动区域
        self.canvas = tk.Canvas(self, bg='#f0f0f0')
        self.scrollbar = ttk.Scrollbar(self, orient=tk.VERTICAL, command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.scrollbar.set)

        self.scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        # 内部容器 Frame (放在 Canvas 中)
        self.inner_frame = ttk.Frame(self.canvas)
        self.canvas.create_window((0, 0), window=self.inner_frame, anchor='nw')

        # 绑定事件：当内部 Frame 尺寸改变时，更新 Canvas 滚动区域
        self.inner_frame.bind("<Configure>", self.on_frame_configure)

        # 绑定主窗口获得焦点事件 -> 自动刷新
        self.bind("<FocusIn>", self.on_focus)

        # 初次加载
        self.after(100, self.refresh)   # 等待 GUI 完全启动

    def on_frame_configure(self, event):
        """更新 Canvas 滚动区域以匹配内部 Frame 大小"""
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def on_focus(self, event):
        """当主窗口获得焦点时刷新并清空搜索"""
        self.search_var.set("")
        self.refresh()

    def refresh(self):
        """刷新窗口列表和预览"""
        # 清空搜索框
        self.search_var.set("")

        # 清空内部 Frame 中的所有子组件
        for widget in self.inner_frame.winfo_children():
            widget.destroy()
        self.window_items.clear()

        # 获取所有窗口
        windows = get_window_list()
        if not windows:
            label = ttk.Label(self.inner_frame, text="没有找到任何窗口")
            label.pack(pady=20)
            return

        # 为每个窗口截图并创建预览
        row = 0
        col = 0
        max_cols = 4   # 每行 4 个预览
        thumb_width = 180
        thumb_height = 120

        for win_id, title in windows:
            # 截图
            img = capture_window(win_id)
            if img is None:
                # 截图失败时使用空白图像
                img = Image.new('RGB', (thumb_width, thumb_height), color='gray')
            else:
                # 缩略图：保持宽高比填充到固定尺寸
                img.thumbnail((thumb_width, thumb_height), Image.LANCZOS)
                # 创建一个固定大小的画布，居中放置缩略图
                thumb = Image.new('RGB', (thumb_width, thumb_height), color='white')
                x = (thumb_width - img.width) // 2
                y = (thumb_height - img.height) // 2
                thumb.paste(img, (x, y))
                img = thumb

            # 转换为 PhotoImage
            photo = ImageTk.PhotoImage(img)

            # 创建 Frame 作为单个预览容器
            preview_frame = ttk.Frame(self.inner_frame, relief=tk.RAISED, borderwidth=1)
            preview_frame.grid(row=row, column=col, padx=5, pady=5, sticky='nsew')

            # 图片 Label
            img_label = ttk.Label(preview_frame, image=photo)
            img_label.image = photo   # 保持引用
            img_label.pack()

            # 标题 Label（截断过长）
            disp_title = (title[:30] + '...') if len(title) > 30 else title
            title_label = ttk.Label(preview_frame, text=disp_title, wraplength=thumb_width)
            title_label.pack()

            # 点击预览 -> 激活窗口并隐藏本工具
            preview_frame.bind("<Button-1>", lambda e, wid=win_id: self.on_click_preview(wid))
            img_label.bind("<Button-1>", lambda e, wid=win_id: self.on_click_preview(wid))
            title_label.bind("<Button-1>", lambda e, wid=win_id: self.on_click_preview(wid))

            # 存储信息
            self.window_items.append({
                'frame': preview_frame,
                'win_id': win_id,
                'title': title,
                'img_label': img_label,
                'title_label': title_label
            })

            # 更新行列
            col += 1
            if col >= max_cols:
                col = 0
                row += 1

        # 配置列的权重，使预览均匀分布
        for c in range(max_cols):
            self.inner_frame.columnconfigure(c, weight=1)

        # 更新滚动区域
        self.inner_frame.update_idletasks()
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def on_click_preview(self, win_id):
        """点击预览：激活窗口，隐藏本工具"""
        activate_window(win_id)
        self.withdraw()   # 隐藏主窗口

    def on_search(self, event):
        """搜索框输入时过滤预览"""
        keyword = self.search_var.get().strip().lower()
        for item in self.window_items:
            title_lower = item['title'].lower()
            if keyword == "" or keyword in title_lower:
                item['frame'].grid()
            else:
                item['frame'].grid_remove()

# ---------------------------- 主程序入口 ----------------------------
if __name__ == "__main__":
    if not check_dependencies():
        sys.exit(1)

    app = WindowPreviewApp()
    app.mainloop()
