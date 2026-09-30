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
from dulwich import porcelain
import pystray
from PIL import Image, ImageDraw

APP_NAME = "OfficeGitSync"
APP_VERSION = "0.1.2"
CONFIG_FILE_NAME = ".officegitsync.json"

DEFAULT_EXCLUDE_PATTERNS = [
    "~$*", "*.tmp", "*.bak", ".git/*", "*.crdownload"
]

class ConfigManager:
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
                print(f"读取配置失败: {e}")
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
            print(f"写入配置失败: {e}")
            return False

class GitDebounceHandler(FileSystemEventHandler):
    def __init__(self, repo_dir, debounce_seconds=3, exclude_patterns=None, on_commit_callback=None):
        super().__init__()
        self.repo_dir = repo_dir
        self.debounce_seconds = debounce_seconds
        self.exclude_patterns = exclude_patterns or list(DEFAULT_EXCLUDE_PATTERNS)
        self.on_commit_callback = on_commit_callback
        self.timer = None
        self.lock = threading.Lock()
        self.repo = Repo.init(repo_dir)

    def is_excluded(self, path):
        rel_path = os.path.relpath(path, self.repo_dir).replace("\\", "/")
        filename = os.path.basename(path)

        if filename == CONFIG_FILE_NAME or rel_path.startswith(".git"):
            return True

        for pattern in self.exclude_patterns:
            if fnmatch.fnmatch(filename, pattern) or fnmatch.fnmatch(rel_path, pattern):
                return True
        return False

    def on_any_event(self, event):
        if event.is_directory or self.is_excluded(event.src_path):
            return

        with self.lock:
            if self.timer:
                self.timer.cancel()
            self.timer = threading.Timer(self.debounce_seconds, self.commit_changes)
            self.timer.start()

    def commit_changes(self):
        with self.lock:
            try:
                self.repo.stage(list(self.get_untracked_files()))
                commit_msg = f"Auto-backup: {time.strftime('%Y-%m-%d %H:%M:%S')}".encode('utf-8')
                self.repo.do_commit(commit_msg, committer=b"OfficeGitSync <backup@local>")
                print(f"[{time.strftime('%H:%M:%S')}] 自动备份完成")
                if self.on_commit_callback:
                    self.on_commit_callback()
            except Exception as e:
                print(f"提交备份失败: {e}")

    def get_untracked_files(self):
        untracked = []
        for root, dirs, files in os.walk(self.repo_dir):
            if ".git" in dirs:
                dirs.remove(".git")
            for file in files:
                full_path = os.path.join(root, file)
                if not self.is_excluded(full_path):
                    untracked.append(os.path.relpath(full_path, self.repo_dir))
        return untracked

class BackupApp:
    def __init__(self, root):
        self.root = root
        self.root.title(f"{APP_NAME} v{APP_VERSION}")
        self.root.geometry("620x580")
        
        self.observer = None
        self.event_handler = None
        self.watch_dir = ""
        self.exclude_patterns = list(DEFAULT_EXCLUDE_PATTERNS)
        self.history_commits = []

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

        # 2. 选项卡界面 (历史版本管理 & 排除规则设置)
        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill="both", expand=True, padx=10, pady=5)

        # Tab 1: 历史版本管理
        self.tab_history = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(self.tab_history, text=" 历史版本管理 ")
        self.setup_history_tab()

        # Tab 2: 排除规则设置
        self.tab_exclude = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(self.tab_exclude, text=" 排除规则设置 ")
        self.setup_exclude_tab()

        # 3. 底部状态与启动控制栏
        btn_frame = ttk.Frame(self.root, padding=10)
        btn_frame.pack(fill="x", padx=10)

        self.btn_toggle = ttk.Button(btn_frame, text="开启无感自动备份", command=self.toggle_monitoring)
        self.btn_toggle.pack(side="left", fill="x", expand=True, padx=5)

        self.lbl_status = ttk.Label(self.root, text="状态: 未运行", foreground="gray")
        self.lbl_status.pack(pady=5)

    def setup_history_tab(self):
        """历史版本界面构建"""
        lbl_tip = ttk.Label(self.tab_history, text="双击或选中历史提交记录可进行版本还原：")
        lbl_tip.pack(anchor="w", pady=(0, 5))

        list_frame = ttk.Frame(self.tab_history)
        list_frame.pack(fill="both", expand=True)

        self.history_listbox = tk.Listbox(list_frame, selectmode=tk.SINGLE)
        self.history_listbox.pack(side="left", fill="both", expand=True)

        scrollbar = ttk.Scrollbar(list_frame, orient="vertical", command=self.history_listbox.yview)
        scrollbar.pack(side="right", fill="y")
        self.history_listbox.config(yscrollcommand=scrollbar.set)

        ctrl_frame = ttk.Frame(self.tab_history)
        ctrl_frame.pack(fill="x", pady=(10, 0))

        btn_refresh = ttk.Button(ctrl_frame, text="刷新历史列表", command=self.load_history)
        btn_refresh.pack(side="left", padx=5)

        btn_revert = ttk.Button(ctrl_frame, text="恢复至选中版本", command=self.revert_to_selected)
        btn_revert.pack(side="right", padx=5)

    def setup_exclude_tab(self):
        """排除规则界面构建"""
        preset_frame = ttk.Frame(self.tab_exclude)
        preset_frame.pack(fill="x", pady=(0, 5))
        
        self.var_office_lock = tk.BooleanVar(value=True)
        chk_office = ttk.Checkbutton(preset_frame, text="过滤 Office/WPS 临时锁文件 (~$*, *.tmp, *.bak)", 
                                     variable=self.var_office_lock, command=self.toggle_preset_rules)
        chk_office.pack(side="left")

        list_frame = ttk.Frame(self.tab_exclude)
        list_frame.pack(fill="both", expand=True, pady=5)

        self.pattern_listbox = tk.Listbox(list_frame, selectmode=tk.SINGLE, height=5)
        self.pattern_listbox.pack(side="left", fill="both", expand=True)

        scrollbar = ttk.Scrollbar(list_frame, orient="vertical", command=self.pattern_listbox.yview)
        scrollbar.pack(side="right", fill="y")
        self.pattern_listbox.config(yscrollcommand=scrollbar.set)

        ctrl_frame = ttk.Frame(self.tab_exclude)
        ctrl_frame.pack(fill="x", pady=(5, 0))

        self.new_pattern_entry = ttk.Entry(ctrl_frame)
        self.new_pattern_entry.pack(side="left", fill="x", expand=True, padx=(0, 5))

        btn_add = ttk.Button(ctrl_frame, text="添加规则", command=self.add_pattern)
        btn_add.pack(side="left", padx=2)

        btn_del = ttk.Button(ctrl_frame, text="删除选中", command=self.remove_pattern)
        btn_del.pack(side="left", padx=2)

    def browse_directory(self):
        selected_dir = filedialog.askdirectory()
        if selected_dir:
            self.watch_dir = selected_dir
            self.dir_entry.delete(0, tk.END)
            self.dir_entry.insert(0, selected_dir)
            
            config = ConfigManager.load_config(selected_dir)
            self.exclude_patterns = config.get("exclude_patterns", list(DEFAULT_EXCLUDE_PATTERNS))
            self.update_pattern_listbox()
            self.load_history()

    def load_history(self):
        """读取 Git Commit Log 并展示在历史列表中"""
        self.history_listbox.delete(0, tk.END)
        self.history_commits.clear()

        if not self.watch_dir or not os.path.exists(os.path.join(self.watch_dir, ".git")):
            self.history_listbox.insert(tk.END, "当前目录未初始化或无历史提交记录")
            return

        try:
            repo = Repo(self.watch_dir)
            walker = repo.get_walker()
            for entry in walker:
                commit = entry.commit
                commit_id = commit.id.decode('utf-8')[:7]
                msg = commit.message.decode('utf-8', errors='ignore').strip()
                commit_time = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(commit.commit_time))
                
                display_str = f"[{commit_time}] ({commit_id}) - {msg}"
                self.history_listbox.insert(tk.END, display_str)
                self.history_commits.append(commit.id)
        except Exception as e:
            self.history_listbox.insert(tk.END, f"读取版本历史出错: {e}")

    def revert_to_selected(self):
        """版本回滚核心方法"""
        selected_indices = self.history_listbox.curselection()
        if not selected_indices:
            messagebox.showwarning("提示", "请先在列表中选中要恢复的历史版本！")
            return

        idx = selected_indices[0]
        if idx >= len(self.history_commits):
            return

        target_commit_id = self.history_commits[idx]
        
        confirm = messagebox.askyesno("确认恢复", f"警告：确定将工作区文件还原到该历史版本？\n当前未保存的修改可能会被覆盖。")
        if not confirm:
            return

        try:
            repo = Repo(self.watch_dir)
            # 使用 Dulwich 彻底重置/检出历史 Commit 的文件
            porcelain.reset(repo, "hard", target_commit_id)
            messagebox.showinfo("成功", "版本还原成功！工作区文件已恢复至选中状态。")
            self.load_history()
        except Exception as e:
            messagebox.showerror("错误", f"版本还原失败: {e}")

    def update_pattern_listbox(self):
        self.pattern_listbox.delete(0, tk.END)
        for pattern in self.exclude_patterns:
            self.pattern_listbox.insert(tk.END, pattern)

    def toggle_preset_rules(self):
        presets = ["~$*", "*.tmp", "*.bak"]
        if self.var_office_lock.get():
            for p in presets:
                if p not in self.exclude_patterns:
                    self.exclude_patterns.append(p)
        else:
            self.exclude_patterns = [p for p in self.exclude_patterns if p not in presets]
        self.update_pattern_listbox()
        self.save_current_config()

    def add_pattern(self):
        new_pattern = self.new_pattern_entry.get().strip()
        if new_pattern and new_pattern not in self.exclude_patterns:
            self.exclude_patterns.append(new_pattern)
            self.update_pattern_listbox()
            self.new_pattern_entry.delete(0, tk.END)
            self.save_current_config()

    def remove_pattern(self):
        selected_indices = self.pattern_listbox.curselection()
        if selected_indices:
            idx = selected_indices[0]
            self.exclude_patterns.pop(idx)
            self.update_pattern_listbox()
            self.save_current_config()

    def save_current_config(self):
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

        self.save_current_config()

        self.event_handler = GitDebounceHandler(
            repo_dir=self.watch_dir,
            debounce_seconds=3,
            exclude_patterns=self.exclude_patterns,
            on_commit_callback=lambda: self.root.after(0, self.load_history)
        )
        self.observer = Observer()
        self.observer.schedule(self.event_handler, self.watch_dir, recursive=True)
        self.observer.start()

        self.btn_toggle.config(text="停止自动备份")
        self.lbl_status.config(text=f"状态: 正在实时监控 [{os.path.basename(self.watch_dir)}]", foreground="green")
        self.load_history()

    def stop_monitoring(self):
        if self.observer:
            self.observer.stop()
            self.observer.join()
            self.observer = None
        self.btn_toggle.config(text="开启无感自动备份")
        self.lbl_status.config(text="状态: 已停止", foreground="gray")

    def create_tray_icon(self):
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
