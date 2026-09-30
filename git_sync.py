import os
import sys
import time
import json
import fnmatch
import socket
import threading
import traceback
import winreg
import tkinter as tk
from tkinter import ttk, messagebox, filedialog, simpledialog
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
from dulwich.repo import Repo
import pystray
from PIL import Image, ImageDraw

APP_NAME = "OfficeGitSync"
APP_VERSION = "0.1.3"
APP_AUTHOR = "Ed Clack"
APP_URL = "https://github.com/fuypck/OfficeGitSync"
CONFIG_FILE_NAME = ".officegitsync.json"
SINGLE_INSTANCE_PORT = 65432  # 用于单实例唤醒的本地通信端口

DEFAULT_EXCLUDE_PATTERNS = [
    "~$*",       # Office/WPS 临时锁文件
    "*.tmp",     # 临时文件
    "*.bak",     # 自动备份文件
    ".git/*",    # Git 核心目录
    "*.crdownload"
]

def log_error(msg, show_ui=True):
    """全局错误日志输出"""
    log_msg = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] ERROR: {msg}\n{traceback.format_exc()}\n"
    print(log_msg)
    
    # 写入日志文件
    try:
        with open("officegitsync.log", "a", encoding="utf-8") as f:
            f.write(log_msg)
    except Exception:
        pass
        
    if show_ui:
        messagebox.showerror("OfficeGitSync 运行错误", f"{msg}\n\n详细信息已记录至 officegitsync.log")

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
                log_error(f"读取配置文件失败: {e}", show_ui=False)
        return {
            "debounce_seconds": 3,
            "exclude_patterns": list(DEFAULT_EXCLUDE_PATTERNS),
            "enable_log": False
        }

    @staticmethod
    def save_config(repo_dir, config_data):
        config_path = ConfigManager.get_config_path(repo_dir)
        try:
            with open(config_path, "w", encoding="utf-8") as f:
                json.dump(config_data, f, ensure_ascii=False, indent=2)
            return True
        except Exception as e:
            log_error(f"保存配置文件失败: {e}")
            return False

class GitDebounceHandler(FileSystemEventHandler):
    def __init__(self, repo_dir, debounce_seconds=3, exclude_patterns=None, trigger_callback=None):
        super().__init__()
        self.repo_dir = repo_dir
        self.debounce_seconds = debounce_seconds
        self.exclude_patterns = exclude_patterns or list(DEFAULT_EXCLUDE_PATTERNS)
        self.trigger_callback = trigger_callback
        self.timer = None
        self.lock = threading.Lock()

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
        if event.is_directory:
            return
        if self.is_excluded(event.src_path):
            return

        with self.lock:
            if self.timer:
                self.timer.cancel()
            self.timer = threading.Timer(self.debounce_seconds, self.on_debounce_success)
            self.timer.start()

    def on_debounce_success(self):
        if self.trigger_callback:
            self.trigger_callback()

class CommitDialog(tk.Toplevel):
    """变动提交弹窗"""
    def __init__(self, parent, repo_dir, changed_files):
        super().__init__(parent)
        self.title("检测到文件变更 - 自动备份确认")
        self.geometry("500x380")
        self.repo_dir = repo_dir
        self.changed_files = changed_files
        self.committed = False

        ttk.Label(self, text="以下文件发生了变更，请输入 Commit 备份说明：", font=("Microsoft YaHei", 10, "bold")).pack(anchor="w", padx=10, pady=10)

        # 变动文件列表展示
        list_frame = ttk.Frame(self)
        list_frame.pack(fill="both", expand=True, padx=10, pady=5)
        
        lb = tk.Listbox(list_frame)
        lb.pack(side="left", fill="both", expand=True)
        for f in changed_files:
            lb.insert(tk.END, f)

        # 提交信息输入
        ttk.Label(self, text="备份说明 (Commit Message):").pack(anchor="w", padx=10, pady=(5, 0))
        self.entry_msg = ttk.Entry(self)
        self.entry_msg.pack(fill="x", padx=10, pady=5)
        self.entry_msg.insert(0, f"Auto-backup: {time.strftime('%Y-%m-%d %H:%M:%S')}")

        btn_frame = ttk.Frame(self)
        btn_frame.pack(fill="x", padx=10, pady=10)
        
        ttk.Button(btn_frame, text="确认备份提交", command=self.do_commit).pack(side="right", padx=5)
        ttk.Button(btn_frame, text="本次忽略", command=self.destroy).pack(side="right")

    def do_commit(self):
        msg = self.entry_msg.get().strip()
        if not msg:
            messagebox.showwarning("警告", "请输入提交说明！")
            return
        try:
            repo = Repo(self.repo_dir)
            repo.stage(self.changed_files)
            repo.do_commit(msg.encode('utf-8'), committer=f"{APP_NAME} <backup@local>".encode('utf-8'))
            self.committed = True
            messagebox.showinfo("成功", "版本提交成功！")
            self.destroy()
        except Exception as e:
            log_error(f"Commit 提交失败: {e}")

class BackupApp:
    def __init__(self, root):
        self.root = root
        self.root.title(f"{APP_NAME} v{APP_VERSION}")
        self.root.geometry("620x560")

        self.observer = None
        self.watch_dir = ""
        self.exclude_patterns = list(DEFAULT_EXCLUDE_PATTERNS)
        self.enable_log = False

        self.setup_ui()
        self.create_tray_icon()
        self.check_autostart_status()

    def setup_ui(self):
        # 1. 目录设置
        dir_frame = ttk.LabelFrame(self.root, text=" 1. 监控工作目录设置 ", padding=10)
        dir_frame.pack(fill="x", padx=10, pady=5)

        self.dir_entry = ttk.Entry(dir_frame)
        self.dir_entry.pack(side="left", fill="x", expand=True, padx=(0, 5))
        ttk.Button(dir_frame, text="浏览...", command=self.browse_directory).pack(side="right")

        # 2. 规则管理
        exclude_frame = ttk.LabelFrame(self.root, text=" 2. 文件过滤/排除规则 ", padding=10)
        exclude_frame.pack(fill="both", expand=True, padx=10, pady=5)

        preset_frame = ttk.Frame(exclude_frame)
        preset_frame.pack(fill="x", pady=(0, 5))
        self.var_office_lock = tk.BooleanVar(value=True)
        ttk.Checkbutton(preset_frame, text="默认排除 Office/WPS 锁文件 (~$*, *.tmp, *.bak)", 
                        variable=self.var_office_lock, command=self.toggle_preset_rules).pack(side="left")

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
        ttk.Button(ctrl_frame, text="删除规则", command=self.remove_pattern).pack(side="left", padx=2)

        # 3. 高级开关设置
        opt_frame = ttk.LabelFrame(self.root, text=" 3. 系统与偏好设置 ", padding=10)
        opt_frame.pack(fill="x", padx=10, pady=5)

        self.var_autostart = tk.BooleanVar(value=False)
        ttk.Checkbutton(opt_frame, text="开机自启动", variable=self.var_autostart, command=self.toggle_autostart).pack(side="left", padx=10)

        self.var_log = tk.BooleanVar(value=False)
        ttk.Checkbutton(opt_frame, text="记录详细 Debug Log", variable=self.var_log, command=self.toggle_log).pack(side="left", padx=10)

        # 4. 控制与历史
        btn_frame = ttk.Frame(self.root, padding=5)
        btn_frame.pack(fill="x", padx=10)

        self.btn_toggle = ttk.Button(btn_frame, text="开启无感自动备份", command=self.toggle_monitoring)
        self.btn_toggle.pack(side="left", fill="x", expand=True, padx=5)

        ttk.Button(btn_frame, text="历史版本管理", command=self.open_history_window).pack(side="right", padx=5)

        self.lbl_status = ttk.Label(self.root, text="状态: 未运行", foreground="gray")
        self.lbl_status.pack(pady=2)

        # 底部版权
        footer = ttk.Label(self.root, text=f"Author: {APP_AUTHOR}  |  GitHub: {APP_URL}", foreground="blue", cursor="hand2")
        footer.pack(side="bottom", pady=5)
        footer.bind("<Button-1>", lambda e: os.system(f"start {APP_URL}"))

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
            
            # 初始化 Git 仓库与配置
            Repo.init(self.watch_dir)
            config = ConfigManager.load_config(selected_dir)
            self.exclude_patterns = config.get("exclude_patterns", list(DEFAULT_EXCLUDE_PATTERNS))
            self.enable_log = config.get("enable_log", False)
            self.var_log.set(self.enable_log)
            self.update_listbox()

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
        p = self.new_pattern_entry.get().strip()
        if p and p not in self.exclude_patterns:
            self.exclude_patterns.append(p)
            self.update_listbox()
            self.new_pattern_entry.delete(0, tk.END)
            self.save_current_config()

    def remove_pattern(self):
        sel = self.pattern_listbox.curselection()
        if sel:
            self.exclude_patterns.pop(sel[0])
            self.update_listbox()
            self.save_current_config()

    def toggle_log(self):
        self.enable_log = self.var_log.get()
        self.save_current_config()

    def save_current_config(self):
        if self.watch_dir and os.path.exists(self.watch_dir):
            config_data = {
                "version": APP_VERSION,
                "debounce_seconds": 3,
                "exclude_patterns": self.exclude_patterns,
                "enable_log": self.enable_log
            }
            ConfigManager.save_config(self.watch_dir, config_data)

    def check_autostart_status(self):
        try:
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run", 0, winreg.KEY_READ)
            winreg.QueryValueEx(key, APP_NAME)
            winreg.CloseKey(key)
            self.var_autostart.set(True)
        except Exception:
            self.var_autostart.set(False)

    def toggle_autostart(self):
        key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
        try:
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_ALL_ACCESS)
            if self.var_autostart.get():
                exe_path = f'"{sys.executable}"' if getattr(sys, 'frozen', False) else f'"{os.path.abspath(__file__)}"'
                winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, exe_path)
            else:
                try:
                    winreg.DeleteValue(key, APP_NAME)
                except FileNotFoundError:
                    pass
            winreg.CloseKey(key)
        except Exception as e:
            log_error(f"开机自启设置失败: {e}")

    def on_file_changed(self):
        """当 watchdog 防抖触发时，弹出提交对话框"""
        try:
            repo = Repo(self.watch_dir)
            status = repo.status()
            
            # 获取所有变更/未追踪的文件
            untracked = []
            handler = GitDebounceHandler(self.watch_dir, exclude_patterns=self.exclude_patterns)
            for root, dirs, files in os.walk(self.watch_dir):
                if ".git" in dirs:
                    dirs.remove(".git")
                for file in files:
                    full_path = os.path.join(root, file)
                    if not handler.is_excluded(full_path):
                        rel_path = os.path.relpath(full_path, self.watch_dir)
                        untracked.append(rel_path)

            if untracked:
                self.root.after(0, lambda: CommitDialog(self.root, self.watch_dir, untracked))
        except Exception as e:
            log_error(f"变动检测触发失败: {e}")

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

        try:
            Repo.init(self.watch_dir)
            self.save_current_config()

            handler = GitDebounceHandler(
                repo_dir=self.watch_dir,
                debounce_seconds=3,
                exclude_patterns=self.exclude_patterns,
                trigger_callback=self.on_file_changed
            )
            self.observer = Observer()
            self.observer.schedule(handler, self.watch_dir, recursive=True)
            self.observer.start()

            self.btn_toggle.config(text="停止自动备份")
            self.lbl_status.config(text=f"状态: 正在实时监控 [{os.path.basename(self.watch_dir)}]", foreground="green")
        except Exception as e:
            log_error(f"启动自动备份失败: {e}")

    def stop_monitoring(self):
        if self.observer:
            self.observer.stop()
            self.observer.join()
            self.observer = None
        self.btn_toggle.config(text="开启无感自动备份")
        self.lbl_status.config(text="状态: 已停止", foreground="gray")

    def open_history_window(self):
        if not self.watch_dir or not os.path.exists(self.watch_dir):
            messagebox.showwarning("提示", "请先选择有效的备份目录！")
            return
        
        hist_win = tk.Toplevel(self.root)
        hist_win.title("历史版本管理与回溯")
        hist_win.geometry("550x350")

        tree = ttk.Treeview(hist_win, columns=("SHA", "Time", "Message"), show="headings")
        tree.heading("SHA", text="版本 Commit")
        tree.heading("Time", text="时间")
        tree.heading("Message", text="提交说明")
        tree.column("SHA", width=80)
        tree.column("Time", width=140)
        tree.column("Message", width=280)
        tree.pack(fill="both", expand=True, padx=10, pady=10)

        try:
            repo = Repo(self.watch_dir)
            walker = repo.get_walker()
            for entry in walker:
                commit = entry.commit
                sha = commit.id.decode('utf-8')[:7]
                msg = commit.message.decode('utf-8').strip()
                ctime = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(commit.commit_time))
                tree.insert("", "end", values=(sha, ctime, msg))
        except Exception as e:
            log_error(f"加载 Git 历史记录失败: {e}")

    def create_tray_icon(self):
        image = Image.new('RGB', (64, 64), color=(0, 120, 215))
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
        self.root.after(0, lambda: (self.root.deiconify(), self.root.lift(), self.root.focus_force()))

    def quit_app(self, icon=None, item=None):
        self.stop_monitoring()
        if self.tray_icon:
            self.tray_icon.stop()
        self.root.after(0, self.root.destroy)

def ensure_single_instance(root):
    """通过 Socket 监听确保软件单一实例运行，重复打开时唤醒已存在界面"""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(('127.0.0.1', SINGLE_INSTANCE_PORT))
        sock.listen(1)

        def listen_wake_up():
            while True:
                conn, _ = sock.accept()
                conn.close()
                root.after(0, lambda: (root.deiconify(), root.lift(), root.focus_force()))

        threading.Thread(target=listen_wake_up, daemon=True).start()
        return True
    except socket.error:
        # 已有实例在运行，通知其唤醒并直接退出当前新进程
        try:
            client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            client.connect(('127.0.0.1', SINGLE_INSTANCE_PORT))
            client.close()
        except Exception:
            pass
        sys.exit(0)

if __name__ == "__main__":
    root = tk.Tk()
    ensure_single_instance(root)
    app = BackupApp(root)
    root.mainloop()
