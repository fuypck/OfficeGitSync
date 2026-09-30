import os
import sys
import time
import json
import fnmatch
import threading
import tkinter as tk
from tkinter import ttk, messagebox, filedialog
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
from dulwich.repo import Repo
import pystray
from PIL import Image, ImageDraw

APP_NAME = "OfficeGitSync"
APP_VERSION = "0.1.2"
CONFIG_FILE_NAME = ".officegitsync.json"

# 默认内置的 Office/WPS 垃圾与临时文件过滤预设规则
DEFAULT_EXCLUDE_PATTERNS = [
    "~$*",       # Office/WPS 临时锁文件
    "*.tmp",     # 临时文件
    "*.bak",     # 自动备份文件
    ".git/*",    # Git 核心版本库目录
    "*.crdownload" # 浏览器未完成下载文件
]

class ConfigManager:
    """管理项目根目录下的配置文件 (.officegitsync.json)"""
    @staticmethod
    def get_config_path(repo_dir):
        return os.path.join(repo_dir, CONFIG_FILE_NAME)

    @staticmethod
    def load_config(repo_dir):
        config_path = ConfigManager.get_config_path(repo_dir)
        if os.path.exists(config_path):
            try:
                with open(config_path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                print(f"读取项目配置失败: {e}")
        # 如果配置文件不存在，返回默认配置
        return {
            "debounce_seconds": 3,
            "exclude_patterns": list(DEFAULT_EXCLUDE_PATTERNS)
        }

    @staticmethod
    def save_config(repo_dir, config_data):
        config_path = ConfigManager.get_config_path(repo_dir)
        try:
            with open(config_path, "w", encoding="utf-8") as f:
                json.dump(config_data, f, ensure_ascii=False, indent=2)
            return True
        except Exception as e:
            print(f"写入项目配置失败: {e}")
            return False

class GitDebounceHandler(FileSystemEventHandler):
    """带防抖与动态通配符过滤功能的 Watchdog 事件监听器"""
    def __init__(self, repo_dir, debounce_seconds=3, exclude_patterns=None):
        super().__init__()
        self.repo_dir = repo_dir
        self.debounce_seconds = debounce_seconds
        self.exclude_patterns = exclude_patterns or list(DEFAULT_EXCLUDE_PATTERNS)
        self.timer = None
        self.lock = threading.Lock()
        self.repo = Repo.init(repo_dir)

    def is_excluded(self, path):
        """检查路径是否命中排除列表（相对路径匹配）"""
        rel_path = os.path.relpath(path, self.repo_dir).replace("\\", "/")
        filename = os.path.basename(path)

        # 忽视配置文件本身以及 .git 目录内变更
        if filename == CONFIG_FILE_NAME or rel_path.startswith(".git"):
            return True

        for pattern in self.exclude_patterns:
            # 支持对文件名或完整相对路径的匹配
            if fnmatch.fnmatch(filename, pattern) or fnmatch.fnmatch(rel_path, pattern):
                return True
        return False

    def on_any_event(self, event):
        if event.is_directory:
            return
        
        # 排除检测
        if self.is_excluded(event.src_path):
            return

        with self.lock:
            if self.timer:
                self.timer.cancel()
            self.timer = threading.Timer(self.debounce_seconds, self.commit_changes)
            self.timer.start()

    def commit_changes(self):
        with self.lock:
            try:
                # 使用 Dulwich 自动阶段化并提交变动
                status = self.repo.status()
                has_changes = any([status.unstaged, status.staged.get('add'), status.staged.get('delete'), status.staged.get('modify')])
                
                # 手动添加未追踪文件与改动文件
                self.repo.stage(list(self.get_untracked_files()))
                
                commit_msg = f"Auto-backup: {time.strftime('%Y-%m-%d %H:%M:%S')}".encode('utf-8')
                self.repo.do_commit(commit_msg, committer=b"OfficeGitSync <backup@local>")
                print(f"[{time.strftime('%H:%M:%S')}] 自动备份完成")
            except Exception as e:
                print(f"提交备份失败: {e}")

    def get_untracked_files(self):
        """获取当前工作区中未排除的文件列表"""
        untracked = []
        for root, dirs, files in os.walk(self.repo_dir):
            # 跳过 .git 目录
            if ".git" in dirs:
                dirs.remove(".git")
            for file in files:
                full_path = os.path.join(root, file)
                if not self.is_excluded(full_path):
                    rel_path = os.path.relpath(full_path, self.repo_dir)
                    untracked.append(rel_path)
        return untracked

class BackupApp:
    def __init__(self, root):
        self.root = root
        self.root.title(f"{APP_NAME} v{APP_VERSION}")
        self.root.geometry("600x520")
        
        self.observer = None
        self.event_handler = None
        self.watch_dir = ""
        self.exclude_patterns = list(DEFAULT_EXCLUDE_PATTERNS)

        self.setup_ui()
        self.create_tray_icon()

    def setup_ui(self):
        # 1. 目录选择区域
        dir_frame = ttk.LabelFrame(self.root, text=" 工作目录设置 ", padding=10)
        dir_frame.pack(fill="x", padx=10, pady=5)

        self.dir_entry = ttk.Entry(dir_frame)
        self.dir_entry.pack(side="left", fill="x", expand=True, padx=(0, 5))
        
        btn_browse = ttk.Button(dir_frame, text="浏览...", command=self.browse_directory)
        btn_browse.pack(side="right")

        # 2. 排除规则设置区域
        exclude_frame = ttk.LabelFrame(self.root, text=" 文件排除/过滤规则 (独立于当前目录) ", padding=10)
        exclude_frame.pack(fill="both", expand=True, padx=10, pady=5)

        # 预设快速勾选框
        preset_frame = ttk.Frame(exclude_frame)
        preset_frame.pack(fill="x", pady=(0, 5))
        
        self.var_office_lock = tk.BooleanVar(value=True)
        chk_office = ttk.Checkbutton(preset_frame, text="过滤 Office/WPS 临时文件 (~$*, *.tmp, *.bak)", 
                                     variable=self.var_office_lock, command=self.toggle_preset_rules)
        chk_office.pack(side="left")

        # 规则列表框 + 滚动条
        list_frame = ttk.Frame(exclude_frame)
        list_frame.pack(fill="both", expand=True, pady=5)

        self.pattern_listbox = tk.Listbox(list_frame, selectmode=tk.SINGLE, height=6)
        self.pattern_listbox.pack(side="left", fill="both", expand=True)

        scrollbar = ttk.Scrollbar(list_frame, orient="vertical", command=self.pattern_listbox.yview)
        scrollbar.pack(side="right", fill="y")
        self.pattern_listbox.config(yscrollcommand=scrollbar.set)

        # 添加与删除规则控制栏
        ctrl_frame = ttk.Frame(exclude_frame)
        ctrl_frame.pack(fill="x", pady=(5, 0))

        self.new_pattern_entry = ttk.Entry(ctrl_frame)
        self.new_pattern_entry.pack(side="left", fill="x", expand=True, padx=(0, 5))
        self.new_pattern_entry.insert(0, "*.pdf") # 默认提示示例

        btn_add = ttk.Button(ctrl_frame, text="添加规则", command=self.add_pattern)
        btn_add.pack(side="left", padx=2)

        btn_del = ttk.Button(ctrl_frame, text="删除选中", command=self.remove_pattern)
        btn_del.pack(side="left", padx=2)

        # 3. 防抖与控制按钮区域
        btn_frame = ttk.Frame(self.root, padding=10)
        btn_frame.pack(fill="x", padx=10)

        self.btn_toggle = ttk.Button(btn_frame, text="开启无感自动备份", command=self.toggle_monitoring)
        self.btn_toggle.pack(side="left", fill="x", expand=True, padx=5)

        self.lbl_status = ttk.Label(self.root, text="状态: 未运行", foreground="gray")
        self.lbl_status.pack(pady=5)

    def update_listbox(self):
        """刷新 UI 中的规则列表"""
        self.pattern_listbox.delete(0, tk.END)
        for pattern in self.exclude_patterns:
            self.pattern_listbox.insert(tk.END, pattern)

    def browse_directory(self):
        selected_dir = filedialog.askdirectory()
        if selected_dir:
            self.watch_dir = selected_dir
            self.dir_entry.delete(0, tk.END)
            self.dir_entry.insert(0, selected_dir)
            
            # 读取该目录专属的配置文件
            config = ConfigManager.load_config(selected_dir)
            self.exclude_patterns = config.get("exclude_patterns", list(DEFAULT_EXCLUDE_PATTERNS))
            self.update_listbox()

    def toggle_preset_rules(self):
        """处理预设勾选框状态变动"""
        presets = ["~$*", "*.tmp", "*.bak"]
        if self.var_office_lock.get():
            for p in presets:
                if p not in self.exclude_patterns:
                    self.exclude_patterns.append(p)
        else:
            self.exclude_patterns = [p for p in self.exclude_patterns if p not in presets]
        self.update_listbox()
        self.save_current_config()

    def add_pattern(self):
        new_pattern = self.new_pattern_entry.get().strip()
        if new_pattern and new_pattern not in self.exclude_patterns:
            self.exclude_patterns.append(new_pattern)
            self.update_listbox()
            self.new_pattern_entry.delete(0, tk.END)
            self.save_current_config()

    def remove_pattern(self):
        selected_indices = self.pattern_listbox.curselection()
        if selected_indices:
            idx = selected_indices[0]
            removed = self.exclude_patterns.pop(idx)
            self.update_listbox()
            self.save_current_config()

    def save_current_config(self):
        """将当前的排除规则写入当前目录的 .officegitsync.json"""
        if self.watch_dir and os.path.exists(self.watch_dir):
            config_data = {
                "version": APP_VERSION,
                "debounce_seconds": 3,
                "exclude_patterns": self.exclude_patterns
            }
            ConfigManager.save_config(self.watch_dir, config_data)

    def toggle_monitoring(self):
        if self.observer and self.observer.is_alive():
            self.stop_monitoring()
        else:
            self.start_monitoring()

    def start_monitoring(self):
        self.watch_dir = self.dir_entry.get().strip()
        if not self.watch_dir or not os.path.exists(self.watch_dir):
            messagebox.showerror("错误", "请选择有效的备份工作目录！")
            return

        # 启动前保存当前规则配置
        self.save_current_config()

        self.event_handler = GitDebounceHandler(
            repo_dir=self.watch_dir,
            debounce_seconds=3,
            exclude_patterns=self.exclude_patterns
        )
        self.observer = Observer()
        self.observer.schedule(self.event_handler, self.watch_dir, recursive=True)
        self.observer.start()

        self.btn_toggle.config(text="停止自动备份")
        self.lbl_status.config(text=f"状态: 正在实时监控 [{os.path.basename(self.watch_dir)}]", foreground="green")

    def stop_monitoring(self):
        if self.observer:
            self.observer.stop()
            self.observer.join()
            self.observer = None
        self.btn_toggle.config(text="开启无感自动备份")
        self.lbl_status.config(text="状态: 已停止", foreground="gray")

    def create_tray_icon(self):
        # 创建默认系统托盘图标
        image = Image.new('RGB', (64, 64), color=(73, 109, 137))
        draw = ImageDraw.Draw(image)
        draw.rectangle([16, 16, 48, 48], fill=(255, 255, 255))

        menu = pystray.Menu(
            pystray.MenuItem("显示界面", self.show_window),
            pystray.MenuItem("退出程序", self.quit_app)
        )
        self.tray_icon = pystray.Icon(APP_NAME, image, APP_NAME, menu)
        threading.Thread(target=self.tray_icon.run, daemon=True).start()

        self.root.protocol('WM_DELETE_WINDOW', self.hide_window)

    def hide_window(self):
        self.root.withdraw()

    def show_window(self, icon=None, item=None):
        self.root.after(0, self.root.deiconify)

    def quit_app(self, icon=None, item=None):
        self.stop_monitoring()
        if self.tray_icon:
            self.tray_icon.stop()
        self.root.after(0, self.root.destroy)

if __name__ == "__main__":
    root = tk.Tk()
    app = BackupApp(root)
    root.mainloop()
