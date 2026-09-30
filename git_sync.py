import os
import sys
import time
import json
import fnmatch
import logging
import threading
import traceback
import winreg
import socket
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
LOG_FILE_NAME = "officegitsync.log"
REG_RUN_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"

# 配置全局日志记录
logging.basicConfig(
    filename=LOG_FILE_NAME,
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    encoding="utf-8"
)

# 默认预设排除规则
DEFAULT_EXCLUDE_PATTERNS = [
    "~$*",       # Office/WPS 锁文件
    "*.tmp",     # 临时文件
    "*.bak",     # 备份文件
    ".git/*",    # Git 版本库
    "*.crdownload"
]

def check_single_instance():
    """使用本地套接字防止重复打开多个后台实例"""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 47128)) # 专属本地通信端口
        return sock
    except socket.error:
        return None

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
                logging.error(f"读取配置失败: {e}\n{traceback.format_exc()}")
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
            logging.error(f"写入配置失败: {e}\n{traceback.format_exc()}")
            return False

class GitDebounceHandler(FileSystemEventHandler):
    def __init__(self, repo_dir, debounce_seconds=3, exclude_patterns=None, error_callback=None):
        super().__init__()
        self.repo_dir = repo_dir
        self.debounce_seconds = debounce_seconds
        self.exclude_patterns = exclude_patterns or list(DEFAULT_EXCLUDE_PATTERNS)
        self.error_callback = error_callback
        self.timer = None
        self.lock = threading.Lock()
        
        try:
            self.repo = Repo.init(repo_dir)
        except Exception as e:
            logging.error(f"初始化仓库失败: {e}\n{traceback.format_exc()}")
            if self.error_callback:
                self.error_callback(f"初始化 Git 仓库失败: {e}")

    def is_excluded(self, path):
        rel_path = os.path.relpath(path, self.repo_dir).replace("\\", "/")
        filename = os.path.basename(path)

        if filename in [CONFIG_FILE_NAME, LOG_FILE_NAME] or rel_path.startswith(".git"):
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
                untracked = self.get_untracked_files()
                if untracked:
                    self.repo.stage(untracked)
                
                commit_msg = f"Auto-backup: {time.strftime('%Y-%m-%d %H:%M:%S')}".encode('utf-8')
                self.repo.do_commit(commit_msg, committer=b"OfficeGitSync <backup@local>")
                logging.info(f"自动备份完成: {time.strftime('%H:%M:%S')}")
            except Exception as e:
                logging.error(f"自动备份失败: {e}\n{traceback.format_exc()}")
                if self.error_callback:
                    self.error_callback(f"自动备份异常: {e}")

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

class HistoryWindow(tk.Toplevel):
    """历史版本管理窗口 (非破坏性版本切换与查看)"""
    def __init__(self, parent, repo_dir):
        super().__init__(parent)
        self.title("历史版本管理")
        self.geometry("550x350")
        self.repo_dir = repo_dir
        self.repo = Repo(repo_dir)
        
        self.setup_ui()
        self.load_history()

    def setup_ui(self):
        lbl_tip = ttk.Label(self, text="提示: 恢复版本将只检出工作区文件，不会删除任何历史提交节点。", foreground="blue")
        lbl_tip.pack(anchor="w", padx=10, pady=5)

        frame = ttk.Frame(self)
        frame.pack(fill="both", expand=True, padx=10, pady=5)

        self.tree = ttk.Treeview(frame, columns=("commit", "time", "message"), show="headings")
        self.tree.heading("commit", text="提交 Hash")
        self.tree.heading("time", text="时间")
        self.tree.heading("message", text="提交信息")
        
        self.tree.column("commit", width=90)
        self.tree.column("time", width=140)
        self.tree.column("message", width=260)

        scrollbar = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        
        self.tree.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        btn_frame = ttk.Frame(self)
        btn_frame.pack(fill="x", padx=10, pady=10)

        ttk.Button(btn_frame, text="恢复至所选版本 (不删历史)", command=self.checkout_version).pack(side="left", padx=5)
        ttk.Button(btn_frame, text="刷新", command=self.load_history).pack(side="right", padx=5)

    def load_history(self):
        for item in self.tree.get_children():
            self.tree.delete(item)

        try:
            walker = self.repo.get_walker()
            for entry in walker:
                commit = entry.commit
                commit_hash = commit.id.decode('utf-8')[:7]
                commit_time = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(commit.commit_time))
                msg = commit.message.decode('utf-8').strip()
                self.tree.insert("", "end", values=(commit_hash, commit_time, msg), tags=(commit.id.decode('utf-8'),))
        except Exception as e:
            messagebox.showerror("错误", f"获取历史版本失败: {e}")

    def checkout_version(self):
        selected = self.tree.selection()
        if not selected:
            messagebox.showwarning("警告", "请先选择一个历史版本！")
            return
            
        commit_full_hash = self.tree.item(selected[0], "tags")[0]
        try:
            # 使用 Dulwich porcelain 还原文件而不丢失链表指针
            porcelain.reset(self.repo, mode="hard", treeish=commit_full_hash.encode('utf-8'))
            messagebox.showinfo("成功", f"工作区已恢复至版本 [{commit_full_hash[:7]}]！所有历史节点保持完好。")
        except Exception as e:
            logging.error(f"恢复版本失败: {e}\n{traceback.format_exc()}")
            messagebox.showerror("错误", f"恢复版本失败: {e}")

class BackupApp:
    def __init__(self, root, start_minimized=False):
        self.root = root
        self.root.title(f"{APP_NAME} v{APP_VERSION}")
        self.root.geometry("620x550")
        
        self.observer = None
        self.event_handler = None
        self.watch_dir = ""
        self.exclude_patterns = list(DEFAULT_EXCLUDE_PATTERNS)

        self.setup_ui()
        self.create_tray_icon()
        self.check_autostart_status()

        if start_minimized:
            self.root.withdraw()

    def setup_ui(self):
        # 1. 工作目录设置
        dir_frame = ttk.LabelFrame(self.root, text=" 工作目录设置 ", padding=10)
        dir_frame.pack(fill="x", padx=10, pady=5)

        self.dir_entry = ttk.Entry(dir_frame)
        self.dir_entry.pack(side="left", fill="x", expand=True, padx=(0, 5))
        
        ttk.Button(dir_frame, text="浏览...", command=self.browse_directory).pack(side="right", padx=2)
        ttk.Button(dir_frame, text="历史版本", command=self.open_history).pack(side="right", padx=2)

        # 2. 自定义与预设排除规则
        exclude_frame = ttk.LabelFrame(self.root, text=" 文件排除/过滤规则管理 ", padding=10)
        exclude_frame.pack(fill="both", expand=True, padx=10, pady=5)

        preset_frame = ttk.Frame(exclude_frame)
        preset_frame.pack(fill="x", pady=(0, 5))
        
        self.var_office_lock = tk.BooleanVar(value=True)
        chk_office = ttk.Checkbutton(preset_frame, text="自动过滤 Office/WPS 锁文件 (~$*, *.tmp, *.bak)", 
                                     variable=self.var_office_lock, command=self.toggle_preset_rules)
        chk_office.pack(side="left")

        list_frame = ttk.Frame(exclude_frame)
        list_frame.pack(fill="both", expand=True, pady=5)

        self.pattern_listbox = tk.Listbox(list_frame, selectmode=tk.SINGLE, height=5)
        self.pattern_listbox.pack(side="left", fill="both", expand=True)

        scrollbar = ttk.Scrollbar(list_frame, orient="vertical", command=self.pattern_listbox.yview)
        scrollbar.pack(side="right", fill="y")
        self.pattern_listbox.config(yscrollcommand=scrollbar.set)

        ctrl_frame = ttk.Frame(exclude_frame)
        ctrl_frame.pack(fill="x", pady=(5, 0))

        self.new_pattern_entry = ttk.Entry(ctrl_frame)
        self.new_pattern_entry.pack(side="left", fill="x", expand=True, padx=(0, 5))

        ttk.Button(ctrl_frame, text="添加规则", command=self.add_pattern).pack(side="left", padx=2)
        ttk.Button(ctrl_frame, text="删除选中", command=self.remove_pattern).pack(side="left", padx=2)

        # 3. 系统配置（开机自启）
        sys_frame = ttk.LabelFrame(self.root, text=" 系统偏好设置 ", padding=10)
        sys_frame.pack(fill="x", padx=10, pady=5)

        self.var_autostart = tk.BooleanVar(value=False)
        ttk.Checkbutton(sys_frame, text="开机自动启动并在后台静默运行", 
                        variable=self.var_autostart, command=self.toggle_autostart).pack(side="left")

        # 4. 控制与状态栏
        btn_frame = ttk.Frame(self.root, padding=10)
        btn_frame.pack(fill="x", padx=10)

        self.btn_toggle = ttk.Button(btn_frame, text="开启无感自动备份", command=self.toggle_monitoring)
        self.btn_toggle.pack(side="left", fill="x", expand=True, padx=5)

        self.lbl_status = ttk.Label(self.root, text="状态: 未运行", foreground="gray")
        self.lbl_status.pack(pady=5)

    def handle_error(self, err_msg):
        """线程安全的全局错误提示"""
        logging.error(err_msg)
        self.root.after(0, lambda: messagebox.showerror("运行异常", f"{err_msg}\n详细日志请参阅 officegitsync.log"))

    def update_listbox(self):
        self.pattern_listbox.delete(0, tk.END)
        for pattern in self.exclude_patterns:
            self.pattern_listbox.insert(tk.END, pattern)

    def browse_directory(self):
        selected_dir = filedialog.askdirectory()
        if selected_dir:
            self.watch_dir = selected_dir
            self.dir_entry.delete(0, tk.END)
            self.dir_entry.insert(0, selected_dir)
            
            config = ConfigManager.load_config(selected_dir)
            self.exclude_patterns = config.get("exclude_patterns", list(DEFAULT_EXCLUDE_PATTERNS))
            self.update_listbox()

    def open_history(self):
        watch_dir = self.dir_entry.get().strip()
        if not watch_dir or not os.path.exists(watch_dir):
            messagebox.showwarning("警告", "请先选择有效的工作目录！")
            return
        HistoryWindow(self.root, watch_dir)

    def toggle_preset_rules(self):
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
        selected = self.pattern_listbox.curselection()
        if selected:
            self.exclude_patterns.pop(selected[0])
            self.update_listbox()
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
            messagebox.showerror("错误", "指定的工作目录不存在，无法开启监控！")
            return

        try:
            self.save_current_config()
            self.event_handler = GitDebounceHandler(
                repo_dir=self.watch_dir,
                debounce_seconds=3,
                exclude_patterns=self.exclude_patterns,
                error_callback=self.handle_error
            )
            self.observer = Observer()
            self.observer.schedule(self.event_handler, self.watch_dir, recursive=True)
            self.observer.start()

            self.btn_toggle.config(text="停止自动备份")
            self.lbl_status.config(text=f"状态: 正在监控 [{os.path.basename(self.watch_dir)}]", foreground="green")
        except Exception as e:
            self.handle_error(f"启动服务失败: {e}\n{traceback.format_exc()}")

    def stop_monitoring(self):
        if self.observer:
            try:
                self.observer.stop()
                self.observer.join()
            except Exception as e:
                logging.error(f"停止服务异常: {e}")
            self.observer = None
        self.btn_toggle.config(text="开启无感自动备份")
        self.lbl_status.config(text="状态: 已停止", foreground="gray")

    def check_autostart_status(self):
        try:
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, REG_RUN_PATH, 0, winreg.KEY_READ)
            value, _ = winreg.QueryValueEx(key, APP_NAME)
            winreg.CloseKey(key)
            self.var_autostart.set(True)
        except WindowsError:
            self.var_autostart.set(False)

    def toggle_autostart(self):
        exe_path = f'"{os.path.abspath(sys.argv[0])}" --minimized'
        try:
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, REG_RUN_PATH, 0, winreg.KEY_WRITE)
            if self.var_autostart.get():
                winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, exe_path)
            else:
                try:
                    winreg.DeleteValue(key, APP_NAME)
                except WindowsError:
                    pass
            winreg.CloseKey(key)
        except Exception as e:
            self.handle_error(f"修改注册表开机项失败: {e}")

    def create_tray_icon(self):
        image = Image.new('RGB', (64, 64), color=(73, 109, 137))
        draw = ImageDraw.Draw(image)
        draw.rectangle([16, 16, 48, 48], fill=(255, 255, 255))

        menu = pystray.Menu(
            pystray.MenuItem("显示主窗口", self.show_window),
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
    # 单实例唯一性校验
    instance_socket = check_single_instance()
    if not instance_socket:
        # 如果已被多开，弹出简单警告框并退出
        root = tk.Tk()
        root.withdraw()
        messagebox.showwarning("提示", f"{APP_NAME} 已经在后台运行中，请检查系统托盘区域！")
        sys.exit(0)

    start_minimized = "--minimized" in sys.argv
    root = tk.Tk()
    app = BackupApp(root, start_minimized=start_minimized)
    root.mainloop()
