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
from tkinter import ttk, messagebox, filedialog

# --- 全局标准流重定向（修复 Wine/PyInstaller --noconsole 下 Dulwich 的 NoneType 报错） ---
class NullWriter:
    def write(self, text): pass
    def flush(self): pass

if sys.stdout is None:
    sys.stdout = NullWriter()
if sys.stderr is None:
    sys.stderr = NullWriter()

from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
from dulwich.repo import Repo
from dulwich import porcelain
import pystray
from PIL import Image, ImageDraw

APP_NAME = "OfficeGitSync"
APP_VERSION = "0.1.3"
APP_AUTHOR = "Ed Clack"
APP_URL = "https://github.com/fuypck/OfficeGitSync"
CONFIG_FILE_NAME = ".officegitsync.json"
SINGLE_INSTANCE_PORT = 65432  # 单实例通信端口

DEFAULT_EXCLUDE_PATTERNS = [
    "~$*",       # Office/WPS 临时锁文件
    "*.tmp",     # 临时文件
    "*.bak",     # 自动备份文件
    ".git/*",    # Git 核心目录
    "*.crdownload"
]

def get_app_dir():
    """获取软件本体可执行文件所在的真实路径（确保日志和配置只存本体目录）"""
    if getattr(sys, 'frozen', False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))

def log_message(msg, enable_log=False):
    """日志输出控制（保存在软件本体目录）"""
    if enable_log:
        try:
            log_path = os.path.join(get_app_dir(), "officegitsync.log")
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")
        except Exception:
            pass

def show_error(title, msg, err_detail="", enable_log=False):
    """统一错误提示框"""
    full_msg = f"{msg}\n\n错误详情: {err_detail}" if err_detail else msg
    if enable_log:
        log_message(f"ERROR: {full_msg}\n{traceback.format_exc()}", enable_log=True)
    messagebox.showerror(title, full_msg)

class AppConfigManager:
    """配置文件管理器：配置文件保存在软件本体目录，绝不污染工作目录"""
    @staticmethod
    def get_config_path():
        return os.path.join(get_app_dir(), CONFIG_FILE_NAME)

    @staticmethod
    def load_config():
        config_path = AppConfigManager.get_config_path()
        if os.path.exists(config_path):
            try:
                with open(config_path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        return {
            "watch_dir": "",
            "debounce_seconds": 3,
            "exclude_patterns": list(DEFAULT_EXCLUDE_PATTERNS),
            "enable_log": False
        }

    @staticmethod
    def save_config(config_data):
        config_path = AppConfigManager.get_config_path()
        try:
            with open(config_path, "w", encoding="utf-8") as f:
                json.dump(config_data, f, ensure_ascii=False, indent=2)
            return True
        except Exception:
            return False

class GitDebounceHandler(FileSystemEventHandler):
    """带防抖与文件匹配过滤的 Watchdog 监听器"""
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

        if rel_path.startswith(".git"):
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
    """文件变更自动弹出的 Commit 对话框"""
    def __init__(self, parent, repo_dir, changed_files, enable_log=False):
        super().__init__(parent)
        self.title("检测到文件变更 - 备份确认")
        self.geometry("520x420")
        self.repo_dir = repo_dir
        self.changed_files = changed_files
        self.enable_log = enable_log

        self.attributes("-topmost", True)
        self.focus_force()

        ttk.Label(self, text="以下文件发生了修改/更新，请确认备份：", font=("Microsoft YaHei", 9, "bold")).pack(anchor="w", padx=10, pady=8)

        list_frame = ttk.Frame(self)
        list_frame.pack(fill="both", expand=True, padx=10, pady=5)
        
        lb = tk.Listbox(list_frame, height=8)
        lb.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(list_frame, orient="vertical", command=lb.yview)
        sb.pack(side="right", fill="y")
        lb.config(yscrollcommand=sb.set)

        for f in changed_files:
            lb.insert(tk.END, f)

        ttk.Label(self, text="提交说明 (Commit Message):").pack(anchor="w", padx=10, pady=(5, 0))
        self.entry_msg = ttk.Entry(self)
        self.entry_msg.pack(fill="x", padx=10, pady=5)
        self.entry_msg.insert(0, f"Auto-backup: {time.strftime('%Y-%m-%d %H:%M:%S')}")

        btn_frame = ttk.Frame(self)
        btn_frame.pack(fill="x", padx=10, pady=10)
        
        ttk.Button(btn_frame, text="提交备份", command=self.do_commit).pack(side="right", padx=5)
        ttk.Button(btn_frame, text="暂不提交", command=self.destroy).pack(side="right")

    def do_commit(self):
        msg = self.entry_msg.get().strip()
        if not msg:
            messagebox.showwarning("提示", "请输入备份说明！", parent=self)
            return
        try:
            porcelain.add(self.repo_dir, self.changed_files)
            porcelain.commit(
                self.repo_dir, 
                message=msg.encode('utf-8'), 
                author=f"{APP_NAME} <backup@local>".encode('utf-8')
            )
            log_message(f"成功提交 Commit: {msg}", self.enable_log)
            messagebox.showinfo("成功", "工作文档已完成备份提交！", parent=self)
            self.destroy()
        except Exception as e:
            show_error("提交失败", "执行 Git Commit 时发生错误", str(e), self.enable_log)

class HistoryWindow(tk.Toplevel):
    """历史版本管理窗口（可选择恢复与手动删除任意版本）"""
    def __init__(self, parent, repo_dir, enable_log=False):
        super().__init__(parent)
        self.title("历史版本管理与回溯")
        self.geometry("640x430")
        self.repo_dir = repo_dir
        self.enable_log = enable_log

        self.tree = ttk.Treeview(self, columns=("SHA", "Time", "Message"), show="headings")
        self.tree.heading("SHA", text="Commit SHA")
        self.tree.heading("Time", text="备份时间")
        self.tree.heading("Message", text="提交说明")
        self.tree.column("SHA", width=90)
        self.tree.column("Time", width=150)
        self.tree.column("Message", width=340)
        
        self.tree.pack(fill="both", expand=True, padx=10, pady=10)

        btn_frame = ttk.Frame(self)
        btn_frame.pack(fill="x", padx=10, pady=(0, 10))

        ttk.Button(btn_frame, text="恢复到此版本", command=self.restore_version).pack(side="left", padx=5)
        ttk.Button(btn_frame, text="删除选中版本", command=self.delete_version).pack(side="left", padx=5)
        ttk.Button(btn_frame, text="刷新列表", command=self.load_history).pack(side="right", padx=5)

        self.load_history()

    def load_history(self):
        for item in self.tree.get_children():
            self.tree.delete(item)
        try:
            repo = Repo(self.repo_dir)
            
            # --- 安全判断：如果仓库尚未产生任何 Commit 提交，避开 KeyError: b'HEAD' ---
            try:
                head_sha = repo.head()
            except KeyError:
                # 没有任何 commit 时 HEAD 不存在，不报错，直接为空提示
                return

            walker = repo.get_walker()
            for entry in walker:
                commit = entry.commit
                full_sha = commit.id.decode('utf-8') if isinstance(commit.id, bytes) else str(commit.id)
                short_sha = full_sha[:7]
                
                raw_msg = commit.message
                msg = raw_msg.decode('utf-8', errors='ignore').strip() if isinstance(raw_msg, bytes) else str(raw_msg).strip()
                
                ctime = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(commit.commit_time))
                self.tree.insert("", "end", iid=full_sha, values=(short_sha, ctime, msg))
        except Exception as e:
            show_error("加载历史失败", "无法读取 Git 提交历史记录", str(e), self.enable_log)

    def restore_version(self):
        selected = self.tree.selection()
        if not selected:
            messagebox.showwarning("提示", "请先选择要恢复的历史版本！", parent=self)
            return
        
        target_sha = selected[0]
        if messagebox.askyesno("确认恢复", f"是否将工作区还原至版本 [{target_sha[:7]}]？\n\n注意：其它历史版本的提交节点均会被保留。", parent=self):
            try:
                porcelain.reset(self.repo_dir, "hard", target_sha)
                messagebox.showinfo("成功", f"工作区已成功还原到版本 [{target_sha[:7]}]！", parent=self)
                self.load_history()
            except Exception as e:
                show_error("还原失败", "恢复指定版本时出错", str(e), self.enable_log)

    def delete_version(self):
        selected = self.tree.selection()
        if not selected:
            messagebox.showwarning("提示", "请先选择要删除的历史版本！", parent=self)
            return

        target_sha = selected[0]
        if messagebox.askyesno("确认删除", f"确定要删除历史版本 [{target_sha[:7]}] 吗？", parent=self):
            try:
                repo = Repo(self.repo_dir)
                try:
                    head_sha = repo.head().decode('utf-8') if isinstance(repo.head(), bytes) else str(repo.head())
                except KeyError:
                    messagebox.showwarning("提示", "当前仓库没有任何可删除的提交！", parent=self)
                    return

                if head_sha == target_sha:
                    messagebox.showwarning("无法删除", "不能直接删除当前的最新版本 HEAD！", parent=self)
                    return

                walker = repo.get_walker()
                commits = [e.commit for e in walker]
                
                new_parent = None
                for c in reversed(commits):
                    c_sha = c.id.decode('utf-8') if isinstance(c.id, bytes) else str(c.id)
                    if c_sha == target_sha:
                        continue
                    if new_parent:
                        c.parents = [new_parent.id]
                    repo.object_store.add_object(c)
                    new_parent = c

                if new_parent:
                    repo.refs[b'refs/heads/master'] = new_parent.id
                    repo.refs[b'HEAD'] = new_parent.id

                messagebox.showinfo("成功", f"已从历史链中移除版本 [{target_sha[:7]}]！", parent=self)
                self.load_history()
            except Exception as e:
                show_error("删除版本失败", "操作提交历史节点时发生错误", str(e), self.enable_log)

class BackupApp:
    def __init__(self, root):
        self.root = root
        self.root.title(f"{APP_NAME} v{APP_VERSION}")
        self.root.geometry("640x580")

        self.observer = None
        self.watch_dir = ""
        self.exclude_patterns = list(DEFAULT_EXCLUDE_PATTERNS)
        self.enable_log = False
        self.dialog_lock = False

        self.load_app_config()
        self.setup_ui()
        self.create_tray_icon()
        self.check_autostart_status()

    def load_app_config(self):
        config = AppConfigManager.load_config()
        self.watch_dir = config.get("watch_dir", "")
        self.exclude_patterns = config.get("exclude_patterns", list(DEFAULT_EXCLUDE_PATTERNS))
        self.enable_log = config.get("enable_log", False)

    def save_app_config(self):
        config_data = {
            "version": APP_VERSION,
            "watch_dir": self.watch_dir,
            "exclude_patterns": self.exclude_patterns,
            "enable_log": self.enable_log
        }
        AppConfigManager.save_config(config_data)

    def setup_ui(self):
        dir_frame = ttk.LabelFrame(self.root, text=" 监控工作目录 ", padding=10)
        dir_frame.pack(fill="x", padx=10, pady=5)

        self.dir_entry = ttk.Entry(dir_frame)
        self.dir_entry.pack(side="left", fill="x", expand=True, padx=(0, 5))
        if self.watch_dir:
            self.dir_entry.insert(0, self.watch_dir)

        ttk.Button(dir_frame, text="浏览选择...", command=self.browse_directory).pack(side="right")

        exclude_frame = ttk.LabelFrame(self.root, text=" 文件过滤与排除规则 ", padding=10)
        exclude_frame.pack(fill="both", expand=True, padx=10, pady=5)

        preset_frame = ttk.Frame(exclude_frame)
        preset_frame.pack(fill="x", pady=(0, 5))
        self.var_office_lock = tk.BooleanVar(value=True)
        ttk.Checkbutton(preset_frame, text="勾选排除 Office/WPS 临时文件 (~$*, *.tmp, *.bak)", 
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
        ttk.Button(ctrl_frame, text="删除选中", command=self.remove_pattern).pack(side="left", padx=2)

        opt_frame = ttk.LabelFrame(self.root, text=" 偏好与功能设置 ", padding=10)
        opt_frame.pack(fill="x", padx=10, pady=5)

        self.var_autostart = tk.BooleanVar(value=False)
        ttk.Checkbutton(opt_frame, text="开机自启动", variable=self.var_autostart, command=self.toggle_autostart).pack(side="left", padx=10)

        self.var_log = tk.BooleanVar(value=self.enable_log)
        ttk.Checkbutton(opt_frame, text="输出 Log 日志 (默认关闭)", variable=self.var_log, command=self.toggle_log).pack(side="left", padx=10)

        btn_frame = ttk.Frame(self.root, padding=5)
        btn_frame.pack(fill="x", padx=10)

        self.btn_toggle = ttk.Button(btn_frame, text="开启无感自动备份", command=self.toggle_monitoring)
        self.btn_toggle.pack(side="left", fill="x", expand=True, padx=5)

        ttk.Button(btn_frame, text="历史版本管理", command=self.open_history_window).pack(side="right", padx=5)

        self.lbl_status = ttk.Label(self.root, text="状态: 未启动", foreground="gray")
        self.lbl_status.pack(pady=3)

        footer_frame = ttk.Frame(self.root, padding=5)
        footer_frame.pack(side="bottom", fill="x")
        
        author_lbl = ttk.Label(footer_frame, text=f"Author: {APP_AUTHOR}", font=("Microsoft YaHei", 9))
        author_lbl.pack(side="left", padx=10)

        url_lbl = ttk.Label(footer_frame, text=APP_URL, foreground="blue", cursor="hand2", font=("Microsoft YaHei", 9, "underline"))
        url_lbl.pack(side="right", padx=10)
        url_lbl.bind("<Button-1>", lambda e: os.system(f"start {APP_URL}"))

        self.update_listbox()

    def update_listbox(self):
        self.pattern_listbox.delete(0, tk.END)
        for pattern in self.exclude_patterns:
            self.pattern_listbox.insert(tk.END, pattern)

    def check_and_init_repo(self, target_dir):
        git_dir = os.path.join(target_dir, ".git")
        if os.path.exists(git_dir):
            confirm = messagebox.askyesno(
                "导入现有 Git 仓库", 
                f"目录 [{os.path.basename(target_dir)}] 下已存在 .git 目录。\n\n是否导入并继承已有版本历史？"
            )
            if not confirm:
                return False
            try:
                Repo(target_dir)
                return True
            except Exception as e:
                show_error("打开仓库失败", "无法读取已存在的 Git 版本库", str(e), self.enable_log)
                return False

        try:
            Repo.init(target_dir)
            return True
        except Exception as e:
            show_error("初始化仓库失败", "无法创建 Git 版本库", str(e), self.enable_log)
            return False

    def browse_directory(self):
        selected_dir = filedialog.askdirectory()
        if selected_dir:
            if self.check_and_init_repo(selected_dir):
                self.watch_dir = selected_dir
                self.dir_entry.delete(0, tk.END)
                self.dir_entry.insert(0, selected_dir)
                self.save_app_config()

    def toggle_preset_rules(self):
        presets = ["~$*", "*.tmp", "*.bak"]
        if self.var_office_lock.get():
            for p in presets:
                if p not in self.exclude_patterns:
                    self.exclude_patterns.append(p)
        else:
            self.exclude_patterns = [p for p in self.exclude_patterns if p not in presets]
        self.update_listbox()
        self.save_app_config()

    def add_pattern(self):
        p = self.new_pattern_entry.get().strip()
        if p and p not in self.exclude_patterns:
            self.exclude_patterns.append(p)
            self.update_listbox()
            self.new_pattern_entry.delete(0, tk.END)
            self.save_app_config()

    def remove_pattern(self):
        sel = self.pattern_listbox.curselection()
        if sel:
            self.exclude_patterns.pop(sel[0])
            self.update_listbox()
            self.save_app_config()

    def toggle_log(self):
        self.enable_log = self.var_log.get()
        self.save_app_config()

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
            show_error("设置失败", "无法修改注册表开机自启项", str(e), self.enable_log)

    def on_file_changed(self):
        if self.dialog_lock:
            return

        def show_dialog():
            try:
                self.dialog_lock = True
                untracked = []
                handler = GitDebounceHandler(self.watch_dir, exclude_patterns=self.exclude_patterns)
                for root, dirs, files in os.walk(self.watch_dir):
                    if ".git" in dirs:
                        dirs.remove(".git")
                    for file in files:
                        full_path = os.path.join(root, file)
                        if not handler.is_excluded(full_path):
                            rel_path = os.path.relpath(full_path, self.watch_dir).replace("\\", "/")
                            untracked.append(rel_path)

                if untracked:
                    dlg = CommitDialog(self.root, self.watch_dir, untracked, self.enable_log)
                    self.root.wait_window(dlg)
            except Exception as e:
                show_error("文件变动处理失败", "捕获文件更新事件时发生异常", str(e), self.enable_log)
            finally:
                self.dialog_lock = False

        self.root.after(0, show_dialog)

    def toggle_monitoring(self):
        if self.observer and self.observer.is_alive():
            self.stop_monitoring()
        else:
            self.start_monitoring()

    def start_monitoring(self):
        self.watch_dir = self.dir_entry.get().strip()
        if not self.watch_dir or not os.path.exists(self.watch_dir):
            messagebox.showerror("错误", "请先选择有效的工作备份目录！")
            return

        if not self.check_and_init_repo(self.watch_dir):
            return

        try:
            self.save_app_config()
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
            self.lbl_status.config(text=f"状态: 正在监控 [{os.path.basename(self.watch_dir)}]", foreground="green")
            log_message("开启实时文档监控备份", self.enable_log)
        except Exception as e:
            show_error("启动失败", "无法启动 Watchdog 文件监控", str(e), self.enable_log)

    def stop_monitoring(self):
        if self.observer:
            self.observer.stop()
            self.observer.join()
            self.observer = None
        self.btn_toggle.config(text="开启无感自动备份")
        self.lbl_status.config(text="状态: 已停止", foreground="gray")

    def open_history_window(self):
        if not self.watch_dir or not os.path.exists(self.watch_dir):
            messagebox.showwarning("提示", "请先选择有效的备份工作目录！")
            return
        HistoryWindow(self.root, self.watch_dir, self.enable_log)

    def create_tray_icon(self):
        image = Image.new('RGB', (64, 64), color=(0, 120, 215))
        draw = ImageDraw.Draw(image)
        draw.rectangle([16, 16, 48, 48], fill=(255, 255, 255))

        menu = pystray.Menu(
            pystray.MenuItem("显示主界面", self.show_window),
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
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(('127.0.0.1', SINGLE_INSTANCE_PORT))
        sock.listen(1)

        def listen_wake_up():
            while True:
                try:
                    conn, _ = sock.accept()
                    conn.close()
                    root.after(0, lambda: (root.deiconify(), root.lift(), root.focus_force()))
                except Exception:
                    break

        threading.Thread(target=listen_wake_up, daemon=True).start()
        return True
    except socket.error:
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