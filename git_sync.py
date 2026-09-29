import os
import sys
import json
import socket
import threading
import time
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from PIL import Image, ImageDraw
import pystray

from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
from dulwich.repo import Repo
from dulwich import porcelain

# ==================== 0. 软件元数据配置 ====================
APP_NAME = "WPS/Office Git文档管理工具"
APP_VERSION = "0.1.0"
APP_AUTHOR = "Ed Clack"
APP_DESCRIPTION = "基于git的WPS/Office文档管理工具"

CONFIG_FILE = "config.json"
SINGLE_INSTANCE_PORT = 47829

# Office/WPS/Windows 常见临时文件与无关文件过滤
IGNORED_PATTERNS_PREFIX = ('~$', '.~', '.~lock.', '.~tmp')
IGNORED_PATTERNS_SUFFIX = ('.tmp', '.bak', '.old', '.log', '.swp', '.lock')

def is_ignored_file(file_name):
    """判断是否为 Office/WPS 临时文件或隐藏锁文件"""
    file_name_lower = file_name.lower()
    if file_name.startswith(IGNORED_PATTERNS_PREFIX):
        return True
    if file_name_lower.endswith(IGNORED_PATTERNS_SUFFIX):
        return True
    if file_name == ".git" or file_name.startswith(".git/"):
        return True
    return False

# ==================== 1. 配置管理 ====================
def load_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"monitored_folders": [], "debounce_seconds": 3}

def save_config(config):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)

def normalize_path(path_str):
    if not path_str:
        return ""
    return os.path.abspath(os.path.normpath(path_str))

# ==================== 2. Git 逻辑封装 ====================
def ensure_git_repo(folder_path):
    folder_path = normalize_path(folder_path)
    git_dir = os.path.join(folder_path, ".git")
    if not os.path.exists(git_dir):
        try:
            Repo.init(folder_path)
            return True
        except Exception as e:
            print(f"[Git Init Error] {e}")
            return False
    return True

def get_folder_changed_files(folder_path):
    folder_path = normalize_path(folder_path)
    if not os.path.exists(os.path.join(folder_path, ".git")):
        return []
    
    try:
        r = Repo(folder_path)
        status = porcelain.status(r)
        
        changed_files = set()
        for _, files in status.staged.items():
            for f in files:
                fname = f.decode('utf-8', errors='ignore') if isinstance(f, bytes) else str(f)
                changed_files.add(fname)
                
        for f in status.unstaged:
            fname = f.decode('utf-8', errors='ignore') if isinstance(f, bytes) else str(f)
            changed_files.add(fname)

        for f in status.untracked:
            fname = f.decode('utf-8', errors='ignore') if isinstance(f, bytes) else str(f)
            changed_files.add(fname)

        valid_changed_files = []
        for f in changed_files:
            base_name = os.path.basename(f)
            if not is_ignored_file(base_name):
                valid_changed_files.append(f)

        return valid_changed_files
    except Exception as e:
        print(f"[Git Status Error] {e}")
        return []

def commit_folder_changes(folder_path, changed_files, message):
    folder_path = normalize_path(folder_path)
    try:
        r = Repo(folder_path)
        if not changed_files:
            return False

        porcelain.add(r, paths=changed_files)
        
        if not message.strip():
            message = f"自动备份变更文件: {', '.join([os.path.basename(f) for f in changed_files[:3]])}"
            if len(changed_files) > 3:
                message += f" 等{len(changed_files)}个文件"

        porcelain.commit(r, message=message.encode("utf-8"), committer=f"{APP_AUTHOR} <backup@local>".encode("utf-8"))
        return True
    except Exception as e:
        print(f"[Git Commit Error] {e}")
        return False

def get_git_history(folder_path):
    folder_path = normalize_path(folder_path)
    git_dir = os.path.join(folder_path, ".git")
    if not os.path.exists(git_dir):
        return []
    try:
        r = Repo(folder_path)
        logs = []
        try:
            walker = r.get_walker(max_entries=100)
            for entry in walker:
                commit = entry.commit
                raw_hash = commit.id.decode("utf-8")
                commit_time_struct = time.localtime(commit.commit_time)
                
                # 转换格式：真实 Hash 用于底层的精确恢复，时间戳编号用于界面人性化显示
                commit_time = time.strftime("%Y-%m-%d %H:%M:%S", commit_time_struct)
                timestamp_id = "V" + time.strftime("%Y%m%d_%H%M%S", commit_time_struct)
                
                msg = commit.message.decode("utf-8", errors="ignore").strip()
                logs.append({
                    "raw_hash": raw_hash,
                    "display_id": timestamp_id,
                    "date": commit_time,
                    "msg": msg
                })
        except KeyError:
            pass
        return logs
    except Exception as e:
        print(f"[Git Log Error] {e}")
        return []

def restore_commit_safely(folder_path, commit_hash):
    folder_path = normalize_path(folder_path)
    try:
        r = Repo(folder_path)
        target_commit_bytes = commit_hash.encode("utf-8")
        
        target_commit = r[target_commit_bytes]
        tree_id = target_commit.tree

        r.reset_index(tree_id)
        porcelain.reset(r, mode="hard", treeish=target_commit_bytes)
        
        restore_msg = f"🔄 还原文档状态至历史版本 [{commit_hash[:7]}]"
        porcelain.add(r, paths=".")
        porcelain.commit(r, message=restore_msg.encode("utf-8"), committer=f"{APP_AUTHOR} <backup@local>".encode("utf-8"))
        return True
    except Exception as e:
        print(f"[Git Restore Error] {e}")
        return False

# ==================== 3. Watchdog 事件监听 ====================
class OfficeFileEventHandler(FileSystemEventHandler):
    def __init__(self, folder_path, change_callback):
        super().__init__()
        self.folder_path = folder_path
        self.change_callback = change_callback

    def on_any_event(self, event):
        if event.is_directory:
            return
        
        filename = os.path.basename(event.src_path)
        if is_ignored_file(filename):
            return

        self.change_callback(self.folder_path)

class WatchdogDaemon:
    def __init__(self, root):
        self.root = root
        self.config = load_config()
        self.observer = Observer()
        self.is_popup_open = False
        
        self.pending_folders = set()
        self.debounce_timer = None
        self.lock = threading.Lock()

        self.main_win = MainWindow(self.root, self.config, self.restart_monitoring)
        self.create_tray_icon()

    def on_folder_changed(self, folder_path):
        if self.is_popup_open:
            return

        with self.lock:
            self.pending_folders.add(folder_path)
            if self.debounce_timer:
                self.debounce_timer.cancel()

            debounce_seconds = self.config.get("debounce_seconds", 3)
            self.debounce_timer = threading.Timer(debounce_seconds, self.trigger_popup)
            self.debounce_timer.start()

    def trigger_popup(self):
        with self.lock:
            if not self.pending_folders or self.is_popup_open:
                return
            folders_to_check = list(self.pending_folders)
            self.pending_folders.clear()

        def check_status_in_background():
            folder_changes_map = {}
            for folder in folders_to_check:
                changed_files = get_folder_changed_files(folder)
                if changed_files:
                    folder_changes_map[folder] = changed_files

            if folder_changes_map:
                self.is_popup_open = True
                self.root.after(0, lambda: CombinedCommitWindow(
                    self.root, folder_changes_map, on_close_callback=self.on_popup_closed
                ))

        threading.Thread(target=check_status_in_background, daemon=True).start()

    def on_popup_closed(self):
        self.is_popup_open = False
        self.root.after(0, lambda: self.main_win.load_history(None))

    def restart_monitoring(self, new_config=None):
        if new_config:
            self.config = new_config

        if self.observer.is_alive():
            self.observer.stop()
            self.observer.join()

        self.observer = Observer()
        for folder in self.config.get("monitored_folders", []):
            norm_f = normalize_path(folder)
            if os.path.exists(norm_f):
                ensure_git_repo(norm_f)
                handler = OfficeFileEventHandler(norm_f, self.on_folder_changed)
                self.observer.schedule(handler, norm_f, recursive=True)

        self.observer.start()

    def create_tray_icon(self):
        image = Image.new('RGB', (64, 64), color=(30, 144, 255))
        d = ImageDraw.Draw(image)
        d.text((18, 20), "Git", fill=(255, 255, 255))

        def open_ui(icon, item):
            self.root.after(0, self.main_win.show)

        def on_exit(icon, item):
            if self.observer.is_alive():
                self.observer.stop()
            icon.stop()
            self.root.after(0, self.root.destroy)

        menu = pystray.Menu(
            pystray.MenuItem("💻 打开主控制台", open_ui),
            pystray.MenuItem("❌ 退出程序", on_exit)
        )

        # 鼠标悬停时的备注文字设置
        self.tray_icon = pystray.Icon("OfficeGitSync", image, APP_DESCRIPTION, menu)
        threading.Thread(target=self.tray_icon.run, daemon=True).start()

    def start(self):
        self.restart_monitoring()
        self.main_win.show()

# ==================== 4. UI 界面 ====================
class CombinedCommitWindow:
    def __init__(self, parent_root, folder_changes_map, on_close_callback=None):
        self.folder_changes_map = folder_changes_map
        self.on_close_callback = on_close_callback
        self.entries = {}
        
        self.top = tk.Toplevel(parent_root)
        self.top.title("📝 文档变更自动备份提交")
        self.top.geometry("680x520")
        self.top.attributes("-topmost", True)
        self.top.protocol("WM_DELETE_WINDOW", self.on_cancel)
        
        header_frame = ttk.Frame(self.top, padding=10)
        header_frame.pack(fill="x")
        ttk.Label(header_frame, text="检测到以下文件已更新，请确认并提交备份：", font=("Microsoft YaHei", 10, "bold")).pack(anchor="w")

        batch_frame = ttk.LabelFrame(self.top, text=" 快捷操作 ", padding=10)
        batch_frame.pack(fill="x", padx=10, pady=5)
        
        ttk.Label(batch_frame, text="统一备注:").pack(side="left", padx=5)
        self.batch_entry = ttk.Entry(batch_frame)
        self.batch_entry.pack(side="left", fill="x", expand=True, padx=5)
        ttk.Button(batch_frame, text="应用到所有选中", command=self.apply_batch_message).pack(side="right", padx=5)

        list_container = ttk.Frame(self.top, padding=10)
        list_container.pack(fill="both", expand=True)

        canvas = tk.Canvas(list_container, borderwidth=0, highlightthickness=0)
        scrollbar = ttk.Scrollbar(list_container, orient="vertical", command=canvas.yview)
        self.scrollable_frame = ttk.Frame(canvas)

        self.scrollable_frame.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=self.scrollable_frame, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)

        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        for folder, files in self.folder_changes_map.items():
            self.create_folder_card(folder, files)

        self.bottom_frame = ttk.Frame(self.top, padding=10)
        self.bottom_frame.pack(fill="x")
        
        self.btn_submit = ttk.Button(self.bottom_frame, text="提交选中的变动", command=self.on_submit)
        self.btn_submit.pack(side="right", padx=5)
        ttk.Button(self.bottom_frame, text="暂不提交 (跳过)", command=self.on_cancel).pack(side="right", padx=5)

    def create_folder_card(self, folder, files):
        card = ttk.LabelFrame(self.scrollable_frame, text=f" 📁 目录: {folder} ", padding=8)
        card.pack(fill="x", expand=True, pady=5, padx=5)

        var_check = tk.BooleanVar(value=True)
        chk = ttk.Checkbutton(card, text="提交此目录", variable=var_check)
        chk.grid(row=0, column=0, sticky="w", padx=5)

        ttk.Label(card, text="备注:").grid(row=0, column=1, sticky="w", padx=5)
        entry = ttk.Entry(card, width=40)
        
        default_msg = f"修改了: {', '.join([os.path.basename(f) for f in files[:2]])}"
        if len(files) > 2:
            default_msg += f" 等{len(files)}个文件"
        entry.insert(0, default_msg)
        entry.grid(row=0, column=2, sticky="ew", padx=5)

        files_frame = ttk.Frame(card, padding=(5, 5, 5, 0))
        files_frame.grid(row=1, column=0, columnspan=3, sticky="ew")
        
        ttk.Label(files_frame, text="变动文件清单:", font=("Microsoft YaHei", 8, "bold"), foreground="gray").pack(anchor="w")
        for f in files:
            ttk.Label(files_frame, text=f"  📄 {f}", font=("Microsoft YaHei", 8), foreground="#333333").pack(anchor="w")

        card.columnconfigure(2, weight=1)
        self.entries[folder] = {"check": var_check, "entry": entry, "files": files}

    def apply_batch_message(self):
        msg = self.batch_entry.get().strip()
        if msg:
            for item in self.entries.values():
                if item["check"].get():
                    item["entry"].delete(0, tk.END)
                    item["entry"].insert(0, msg)

    def on_submit(self):
        self.btn_submit.config(state="disabled", text="正在提交备份中...")

        def do_commit_in_background():
            success_count = 0
            for folder, controls in self.entries.items():
                if controls["check"].get():
                    msg = controls["entry"].get().strip()
                    files = controls["files"]
                    if commit_folder_changes(folder, files, msg):
                        success_count += 1

            def on_finished():
                messagebox.showinfo("完成", f"成功完成 {success_count} 个文件夹的备份提交！", parent=self.top)
                self.close_window()

            self.top.after(0, on_finished)

        threading.Thread(target=do_commit_in_background, daemon=True).start()

    def on_cancel(self):
        self.close_window()

    def close_window(self):
        if self.on_close_callback:
            self.on_close_callback()
        self.top.destroy()

class MainWindow:
    def __init__(self, root, config, on_config_change_cb):
        self.root = root
        self.config = config
        self.on_config_change_cb = on_config_change_cb
        self.sort_reverse = {}  # 记录各列的排序顺序 (True 为倒序，False 为正序)

        self.root.title(f"{APP_NAME} v{APP_VERSION} - By {APP_AUTHOR}")
        self.root.geometry("750x520")
        self.root.protocol("WM_DELETE_WINDOW", self.hide_to_tray)

        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill="both", expand=True, padx=10, pady=10)

        self.tab_settings = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(self.tab_settings, text=" ⚙️ 监控目录设置 ")
        self.setup_settings_tab()

        self.tab_history = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(self.tab_history, text=" 📜 历史版本查看与恢复 ")
        self.setup_history_tab()

    def setup_settings_tab(self):
        ttk.Label(self.tab_settings, text="已监控的工作文件夹列表（包含深层子目录）：", font=("Microsoft YaHei", 9, "bold")).pack(anchor="w", pady=(0, 5))

        list_frame = ttk.Frame(self.tab_settings)
        list_frame.pack(fill="both", expand=True)

        self.listbox = tk.Listbox(list_frame, selectmode="single")
        self.listbox.pack(side="left", fill="both", expand=True)

        for f in self.config.get("monitored_folders", []):
            self.listbox.insert(tk.END, f)

        btn_frame = ttk.Frame(list_frame, padding=5)
        btn_frame.pack(side="right", fill="y")

        ttk.Button(btn_frame, text="添加文件夹", command=self.add_folder).pack(fill="x", pady=5)
        ttk.Button(btn_frame, text="移除选中", command=self.remove_folder).pack(fill="x", pady=5)

        bottom = ttk.Frame(self.tab_settings, padding=(0, 10, 0, 0))
        bottom.pack(fill="x")
        
        # 底部标注软件版本与作者
        info_label = ttk.Label(bottom, text=f"版本: {APP_VERSION} | 作者: {APP_AUTHOR}", foreground="gray")
        info_label.pack(side="left")

        ttk.Button(bottom, text="保存设置并生效", command=self.save_settings).pack(side="right")

    def setup_history_tab(self):
        top_frame = ttk.Frame(self.tab_history)
        top_frame.pack(fill="x", pady=(0, 10))

        ttk.Label(top_frame, text="选择要查看的工作文件夹：").pack(side="left", padx=5)
        self.folder_cb = ttk.Combobox(top_frame, values=self.config.get("monitored_folders", []), state="readonly", width=50)
        self.folder_cb.pack(side="left", fill="x", expand=True, padx=5)
        self.folder_cb.bind("<<ComboboxSelected>>", self.load_history)

        table_frame = ttk.Frame(self.tab_history)
        table_frame.pack(fill="both", expand=True)

        columns = ("display_id", "date", "msg")
        self.tree = ttk.Treeview(table_frame, columns=columns, show="headings", selectmode="browse")
        
        # 定义各列，并绑定点击表头排序事件
        self.columns_config = {
            "display_id": "版本ID (时间戳)",
            "date": "提交时间",
            "msg": "提交备注说明"
        }
        
        for col, title in self.columns_config.items():
            self.tree.heading(col, text=f"{title} ↕", command=lambda _col=col: self.sort_column(_col))
            self.sort_reverse[_col] = False

        self.tree.column("display_id", width=160, anchor="center")
        self.tree.column("date", width=160, anchor="center")
        self.tree.column("msg", width=320, anchor="w")

        scrollbar = ttk.Scrollbar(table_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)

        self.tree.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        bottom_frame = ttk.Frame(self.tab_history, padding=(0, 10, 0, 0))
        bottom_frame.pack(fill="x")

        ttk.Button(bottom_frame, text="刷新列表", command=lambda: self.load_history(None)).pack(side="left", padx=5)
        ttk.Button(bottom_frame, text="恢复当前目录到所选历史版本", command=self.restore_selected).pack(side="right", padx=5)
        
        folders = self.config.get("monitored_folders", [])
        if folders:
            self.folder_cb.current(0)
            self.load_history(None)

    def sort_column(self, col):
        """点击表格表头实现排序算法"""
        items = [(self.tree.set(k, col), k) for k in self.tree.get_children('')]
        reverse = not self.sort_reverse[col]
        self.sort_reverse[col] = reverse

        # 进行自然排序
        items.sort(reverse=reverse)

        for index, (val, k) in enumerate(items):
            self.tree.move(k, '', index)

        # 动态更新列头图标表示排序方向
        for c, title in self.columns_config.items():
            if c == col:
                arrow = " ▲" if not reverse else " ▼"
                self.tree.heading(c, text=f"{title}{arrow}")
            else:
                self.tree.heading(c, text=f"{title} ↕")

    def add_folder(self):
        folder = filedialog.askdirectory(parent=self.root, title="选择要监控的工作文件夹")
        if folder:
            folder = normalize_path(folder)
            if folder not in self.listbox.get(0, tk.END):
                self.listbox.insert(tk.END, folder)
                ensure_git_repo(folder)

    def remove_folder(self):
        selected = self.listbox.curselection()
        if selected:
            self.listbox.delete(selected[0])

    def save_settings(self):
        folders = [normalize_path(f) for f in self.listbox.get(0, tk.END)]
        for f in folders:
            ensure_git_repo(f)
        self.config["monitored_folders"] = folders
        save_config(self.config)
        self.folder_cb["values"] = folders
        if folders:
            self.folder_cb.current(0)
            self.load_history(None)
        if self.on_config_change_cb:
            self.on_config_change_cb(self.config)
        messagebox.showinfo("成功", "监控目录设置保存成功！已开启系统级监听！", parent=self.root)

    def load_history(self, event):
        for item in self.tree.get_children():
            self.tree.delete(item)
        selected_folder = self.folder_cb.get()
        if not selected_folder:
            return
        logs = get_git_history(selected_folder)
        for log in logs:
            # item 对应的 text 存真实的 Commit raw_hash，方便还原操作；values 存界面展示内容
            self.tree.insert("", tk.END, text=log["raw_hash"], values=(log["display_id"], log["date"], log["msg"]))

    def restore_selected(self):
        selected_item = self.tree.selection()
        if not selected_item:
            messagebox.showwarning("提示", "请先在列表中选中一个要恢复的历史版本！", parent=self.root)
            return

        commit_hash = self.tree.item(selected_item[0], "text")
        item_values = self.tree.item(selected_item[0], "values")
        display_id, commit_date, commit_msg = item_values[0], item_values[1], item_values[2]
        selected_folder = self.folder_cb.get()

        confirm = messagebox.askyesno(
            "安全还原提示",
            f"您确定要将文件夹：\n{selected_folder}\n\n还原到以下历史版本吗？\n"
            f"版本ID：{display_id}\n时间：{commit_date}\n备注：{commit_msg}\n\n"
            f"✅ 提示：系统将以【追加备份】的方式还原文件，现有的所有 Commit 历史均会完整保留，不会丢失！",
            parent=self.root
        )

        if confirm:
            if restore_commit_safely(selected_folder, commit_hash):
                messagebox.showinfo("成功", f"文件已恢复至版本 [{display_id}] 的状态！并已自动创建还原记录节点。", parent=self.root)
                self.load_history(None)
            else:
                messagebox.showerror("错误", "恢复历史版本失败！", parent=self.root)

    def show(self):
        folders = self.config.get("monitored_folders", [])
        self.folder_cb["values"] = folders
        if folders and not self.folder_cb.get():
            self.folder_cb.current(0)
            self.load_history(None)
        self.root.deiconify()
        self.root.attributes("-topmost", True)
        self.root.attributes("-topmost", False)

    def hide_to_tray(self):
        self.root.withdraw()

# ==================== 5. 单实例通信与启动 ====================
def start_single_instance_listener(root, daemon):
    def listener():
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            server.bind(("127.0.0.1", SINGLE_INSTANCE_PORT))
            server.listen(5)
            while True:
                conn, _ = server.accept()
                msg = conn.recv(1024).decode("utf-8")
                if msg == "WAKE_UP":
                    root.after(0, daemon.main_win.show)
                conn.close()
        except Exception:
            pass

    threading.Thread(target=listener, daemon=True).start()

def try_notify_existing_instance():
    try:
        client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        client.settimeout(1.0)
        client.connect(("127.0.0.1", SINGLE_INSTANCE_PORT))
        client.sendall(b"WAKE_UP")
        client.close()
        return True
    except Exception:
        return False

if __name__ == "__main__":
    if try_notify_existing_instance():
        sys.exit(0)

    root = tk.Tk()
    daemon = WatchdogDaemon(root)
    start_single_instance_listener(root, daemon)
    daemon.start()

    root.mainloop()
