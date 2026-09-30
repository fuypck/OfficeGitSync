import os
import sys
import time
import json
import logging
import traceback
import fnmatch
import threading
import subprocess
import winreg
import tkinter as tk
from tkinter import ttk, messagebox, filedialog, simpledialog
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
from dulwich.repo import Repo
from dulwich.objects import Commit
import pystray
from PIL import Image, ImageDraw

APP_NAME = "OfficeGitSync"
APP_VERSION = "0.1.3"
APP_AUTHOR = "Ed Clack"
APP_URL = "https://github.com/fuypck/OfficeGitSync"
CONFIG_FILE_NAME = ".officegitsync.json"
LOG_FILE_NAME = "OfficeGitSync.log"

# 配置日志系统
logging.basicConfig(
    filename=LOG_FILE_NAME,
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    encoding="utf-8"
)

# 默认内置排除预设
DEFAULT_EXCLUDE_PATTERNS = [
    "~$*",       # Office/WPS 锁文件
    "*.tmp",     # 临时文件
    "*.bak",     # 备份文件
    ".git/*",    # Git 版本库
    "*.crdownload"
]

def ensure_single_instance():
    """Windows 下的简单单例锁，防止多次打开软件后台"""
    import socket
    global lock_socket
    lock_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        lock_socket.bind(('127.0.0.1', 64321))
    except socket.error:
        messagebox.showwarning("提示", f"{APP_NAME} 已经在运行中！")
        sys.exit(0)

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
                logging.error(f"读取配置失败: {e}")
        return {
            "version": APP_VERSION,
            "debounce_seconds": 3,
            "exclude_patterns": list(DEFAULT_EXCLUDE_PATTERNS)
        }

    @staticmethod
    def save_config(repo_dir, config_data):
        config_path = ConfigManager.get_config_path(repo_dir)
        try:
            with open(config_path, "w", encoding="utf-8") as f:
                json.dump(config_data, f, ensure_ascii=False, indent=2)
            logging.info(f"成功保存配置文件至: {config_path}")
            return True
        except Exception as e:
            logging.error(f"保存配置文件失败: {e}")
            return False

class GitDebounceHandler(FileSystemEventHandler):
    def __init__(self, repo_dir, app_callback, debounce_seconds=3, exclude_patterns=None):
        super().__init__()
        self.repo_dir = repo_dir
        self.app_callback = app_callback
        self.debounce_seconds = debounce_seconds
        self.exclude_patterns = exclude_patterns or list(DEFAULT_EXCLUDE_PATTERNS)
        self.timer = None
        self.lock = threading.Lock()
        
        try:
            self.repo = Repo.init(repo_dir)
        except Exception as e:
            logging.error(f"初始化 Git 仓库失败: {e}")
            raise e

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
            self.timer = threading.Timer(self.debounce_seconds, self.trigger_commit_prompt)
            self.timer.start()

    def trigger_commit_prompt(self):
        """文件更新防抖完成后，通知主界面弹出 Commit 对话框"""
        with self.lock:
            self.app_callback()

class BackupApp:
    def __init__(self, root):
        self.root = root
        self.root.title(f"{APP_NAME} v{APP_VERSION}")
        self.root.geometry("640x620")
        
        self.observer = None
        self.event_handler = None
        self.watch_dir = ""
        self.exclude_patterns = list(DEFAULT_EXCLUDE_PATTERNS)

        self.setup_ui()
        self.create_tray_icon()

    def setup_ui(self):
        # 顶部作者与 GitHub 区域
        header_frame = ttk.Frame(self.root, padding=5)
        header_frame.pack(fill="x", padx=10)
        
        lbl_info = ttk.Label(header_frame, text=f"作者: {APP_AUTHOR} | 项目: {APP_URL}", foreground="blue", cursor="hand2")
        lbl_info.pack(side="left")
        lbl_info.bind("<Button-1>", lambda e: os.system(f"start {APP_URL}"))

        # 1. 目录设置 + 开机自启
        dir_frame = ttk.LabelFrame(self.root, text=" 工作目录与偏好设置 ", padding=10)
        dir_frame.pack(fill="x", padx=10, pady=5)

        self.dir_entry = ttk.Entry(dir_frame)
        self.dir_entry.pack(side="left", fill="x", expand=True, padx=(0, 5))
        
        btn_browse = ttk.Button(dir_frame, text="选择目录", command=self.browse_directory)
        btn_browse.pack(side="left", padx=2)

        self.var_autostart = tk.BooleanVar(value=self.check_autostart())
        chk_autostart = ttk.Checkbutton(dir_frame, text="开机自启", variable=self.var_autostart, command=self.toggle_autostart)
        chk_autostart.pack(side="left", padx=5)

        # 2. 排除规则管理
        exclude_frame = ttk.LabelFrame(self.root, text=" 排除规则管理 (.officegitsync.json) ", padding=10)
        exclude_frame.pack(fill="x", padx=10, pady=5)

        preset_frame = ttk.Frame(exclude_frame)
        preset_frame.pack(fill="x", pady=(0, 5))
        
        self.var_office_lock = tk.BooleanVar(value=True)
        chk_office = ttk.Checkbutton(preset_frame, text="预设：过滤 Office/WPS 临时锁文件 (~$*, *.tmp, *.bak)", 
                                     variable=self.var_office_lock, command=self.toggle_preset_rules)
        chk_office.pack(side="left")

        list_frame = ttk.Frame(exclude_frame)
        list_frame.pack(fill="x", pady=5)

        self.pattern_listbox = tk.Listbox(list_frame, height=4)
        self.pattern_listbox.pack(side="left", fill="x", expand=True)

        ctrl_frame = ttk.Frame(exclude_frame)
        ctrl_frame.pack(fill="x", pady=(5, 0))

        self.new_pattern_entry = ttk.Entry(ctrl_frame)
        self.new_pattern_entry.pack(side="left", fill="x", expand=True, padx=(0, 5))
        self.new_pattern_entry.insert(0, "*.iso")

        btn_add = ttk.Button(ctrl_frame, text="添加规则", command=self.add_pattern)
        btn_add.pack(side="left", padx=2)

        btn_del = ttk.Button(ctrl_frame, text="删除选中", command=self.remove_pattern)
        btn_del.pack(side="left", padx=2)

        # 3. 历史版本管理界面
        history_frame = ttk.LabelFrame(self.root, text=" 历史版本管理（还原不删历史，支持指定删除）", padding=10)
        history_frame.pack(fill="both", expand=True, padx=10, pady=5)

        self.history_tree = ttk.Treeview(history_frame, columns=("SHA", "Time", "Message"), show="headings", height=5)
        self.history_tree.heading("SHA", text="版本 ID")
        self.history_tree.heading("Time", text="备份时间")
        self.history_tree.heading("Message", text="提交说明")
        self.history_tree.column("SHA", width=80)
        self.history_tree.column("Time", width=140)
        self.history_tree.column("Message", width=260)
        self.history_tree.pack(side="left", fill="both", expand=True)

        hist_btn_frame = ttk.Frame(history_frame)
        hist_btn_frame.pack(side="right", fill="y", padx=(5, 0))

        btn_refresh = ttk.Button(hist_btn_frame, text="刷新历史", command=self.load_history)
        btn_refresh.pack(fill="x", pady=2)

        btn_restore = ttk.Button(hist_btn_frame, text="还原此版本", command=self.restore_version)
        btn_restore.pack(fill="x", pady=2)

        btn_delete_commit = ttk.Button(hist_btn_frame, text="删除此记录", command=self.delete_version)
        btn_delete_commit.pack(fill="x", pady=2)

        # 4. 控制与状态区域
        btn_frame = ttk.Frame(self.root, padding=5)
        btn_frame.pack(fill="x", padx=10)

        self.btn_toggle = ttk.Button(btn_frame, text="开启无感自动备份", command=self.toggle_monitoring)
        self.btn_toggle.pack(side="left", fill="x", expand=True, padx=5)

        self.lbl_status = ttk.Label(self.root, text="状态: 未运行", foreground="gray")
        self.lbl_status.pack(pady=5)

        self.update_listbox()

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
            self.load_history()

    def save_current_config(self):
        if self.watch_dir and os.path.exists(self.watch_dir):
            config_data = {
                "version": APP_VERSION,
                "debounce_seconds": 3,
                "exclude_patterns": self.exclude_patterns
            }
            ConfigManager.save_config(self.watch_dir, config_data)

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
            idx = selected[0]
            self.exclude_patterns.pop(idx)
            self.update_listbox()
            self.save_current_config()

    def prompt_commit(self):
        """当捕获更新事件时，自动弹窗询问提交信息并触发备份"""
        def _ask():
            try:
                msg = simpledialog.askstring("文件变更检测", "检测到文档更新，请输入提交备注信息（留空自动生成）:", parent=self.root)
                if msg is None:  # 用户点击取消
                    return
                self.do_commit(msg.strip())
            except Exception as e:
                logging.error(f"弹窗 Commit 异常: {e}\n{traceback.format_exc()}")
                messagebox.showerror("错误", f"备份处理出错: {e}")

        self.root.after(0, _ask)

    def do_commit(self, custom_msg=""):
        try:
            repo = Repo(self.watch_dir)
            # 找到更改和未跟踪的文件
            untracked = []
            for root, dirs, files in os.walk(self.watch_dir):
                if ".git" in dirs:
                    dirs.remove(".git")
                for file in files:
                    full_path = os.path.join(root, file)
                    if not self.event_handler.is_excluded(full_path):
                        rel_path = os.path.relpath(full_path, self.watch_dir)
                        untracked.append(rel_path)

            if not untracked:
                return

            repo.stage(untracked)
            commit_text = custom_msg if custom_msg else f"Auto-backup: {time.strftime('%Y-%m-%d %H:%M:%S')}"
            repo.do_commit(commit_text.encode('utf-8'), committer=b"OfficeGitSync <backup@local>")
            
            logging.info(f"成功完成提交: {commit_text}")
            self.root.after(0, self.load_history)
        except Exception as e:
            logging.error(f"Git 提交失败: {e}\n{traceback.format_exc()}")
            messagebox.showerror("提交异常", f"备份提交失败: {e}\n详细信息已写入日志。")

    def load_history(self):
        """加载历史 Git 提交节点"""
        self.history_tree.delete(*self.history_tree.get_children())
        if not self.watch_dir or not os.path.exists(os.path.join(self.watch_dir, ".git")):
            return

        try:
            repo = Repo(self.watch_dir)
            walker = repo.get_walker()
            for entry in walker:
                commit = entry.commit
                sha = commit.id.decode('utf-8')[:7]
                commit_time = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(commit.commit_time))
                message = commit.message.decode('utf-8', errors='ignore').strip()
                self.history_tree.insert("", "end", values=(sha, commit_time, message))
        except Exception as e:
            logging.error(f"读取历史版本失败: {e}")

    def restore_version(self):
        """恢复指定版本但不删除后续版本（Checkout 覆盖）"""
        selected = self.history_tree.selection()
        if not selected:
            messagebox.showwarning("警告", "请先从列表中选择一个历史版本！")
            return

        sha = self.history_tree.item(selected[0])['values'][0]
        if messagebox.askyesno("确认还原", f"确定将文件还原到版本 [{sha}] 吗？\n还原后将自动生成一条新的还原备份，历史版本不会丢失。"):
            try:
                # 使用 git checkout 检出文件，然后再次 commit 作为新的还原节点
                subprocess.run(["git", "checkout", sha, "."], cwd=self.watch_dir, check=True, creationflags=subprocess.CREATE_NO_WINDOW)
                self.do_commit(f"Restore to version [{sha}]")
                messagebox.showinfo("成功", f"已成功还原至版本 [{sha}]！")
            except Exception as e:
                logging.error(f"还原失败: {e}\n{traceback.format_exc()}")
                messagebox.showerror("错误", f"还原版本失败（请确保系统安装有 git 工具）: {e}")

    def delete_version(self):
        """手动删除特定的提交版本"""
        selected = self.history_tree.selection()
        if not selected:
            messagebox.showwarning("警告", "请先选择需要删除的提交记录！")
            return

        sha = self.history_tree.item(selected[0])['values'][0]
        if messagebox.askyesno("危险操作", f"确定要永久删除 Commit [{sha}] 吗？\n警告：此操作不可逆！"):
            try:
                # 使用 rebase 丢弃特定 commit
                cmd = f"git filter-branch -f --commit-filter \"if [ $GIT_COMMIT = {sha} ]; then skip_commit \"$@\"; else git commit-tree \"$@\"; fi\" HEAD"
                subprocess.run(cmd, cwd=self.watch_dir, shell=True, check=True, creationflags=subprocess.CREATE_NO_WINDOW)
                messagebox.showinfo("成功", f"已被移除 Commit [{sha}]！")
                self.load_history()
            except Exception as e:
                logging.error(f"删除 Commit 失败: {e}")
                messagebox.showerror("错误", f"删除失败: {e}")

    def check_autostart(self):
        """检查开机自启注册表"""
        try:
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run", 0, winreg.KEY_READ)
            winreg.QueryValueEx(key, APP_NAME)
            winreg.CloseKey(key)
            return True
        except FileNotFoundError:
            return False

    def toggle_autostart(self):
        """设置/取消开机自启"""
        key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
        try:
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_WRITE)
            if self.var_autostart.get():
                exe_path = sys.executable if getattr(sys, 'frozen', False) else os.path.abspath(__file__)
                winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, f'"{exe_path}"')
                logging.info("开启开机自启功能")
            else:
                winreg.DeleteValue(key, APP_NAME)
                logging.info("关闭开机自启功能")
            winreg.CloseKey(key)
        except Exception as e:
            logging.error(f"修改开机自启设置失败: {e}")

    def toggle_monitoring(self):
        if self.observer and self.observer.is_alive():
            self.stop_monitoring()
        else:
            self.start_monitoring()

    def start_monitoring(self):
        self.watch_dir = self.dir_entry.get().strip()
        if not self.watch_dir or not os.path.exists(self.watch_dir):
            messagebox.showerror("错误", "请先选择有效的工作目录！")
            return

        try:
            self.save_current_config()
            self.event_handler = GitDebounceHandler(
                repo_dir=self.watch_dir,
                app_callback=self.prompt_commit,
                debounce_seconds=3,
                exclude_patterns=self.exclude_patterns
            )
            self.observer = Observer()
            self.observer.schedule(self.event_handler, self.watch_dir, recursive=True)
            self.observer.start()

            self.btn_toggle.config(text="停止自动备份")
            self.lbl_status.config(text=f"状态: 正在运行 [{os.path.basename(self.watch_dir)}]", foreground="green")
            logging.info(f"开启无感自动备份，监控目录: {self.watch_dir}")
        except Exception as e:
            logging.error(f"启动监控失败: {e}\n{traceback.format_exc()}")
            messagebox.showerror("启动失败", f"启动监控时发生异常: {e}\n详见 OfficeGitSync.log")

    def stop_monitoring(self):
        if self.observer:
            self.observer.stop()
            self.observer.join()
            self.observer = None
        self.btn_toggle.config(text="开启无感自动备份")
        self.lbl_status.config(text="状态: 已停止", foreground="gray")
        logging.info("停止无感自动备份")

    def create_tray_icon(self):
        image = Image.new('RGB', (64, 64), color=(30, 144, 255))
        draw = ImageDraw.Draw(image)
        draw.rectangle([16, 16, 48, 48], fill=(255, 255, 255))

        menu = pystray.Menu(
            pystray.MenuItem("打开主界面", self.show_window),
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
    ensure_single_instance()
    root = tk.Tk()
    app = BackupApp(root)
    root.mainloop()
