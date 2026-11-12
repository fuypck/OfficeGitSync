import os
import sys
import time
import json
import fnmatch
import socket
import threading
import traceback
import webbrowser
import winreg
import tkinter as tk
from tkinter import ttk, messagebox, filedialog, simpledialog

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
from dulwich.objects import Commit, Tag, parse_timezone
from dulwich import porcelain
import pystray
from PIL import Image, ImageDraw

APP_NAME = "OfficeGitSync"
APP_VERSION = "0.1.5"
APP_AUTHOR = "Ed Clack"
APP_URL = "https://github.com/fuypck/OfficeGitSync"
LANZOU_URL = "https://wwbvu.lanzoum.com/b00egzj8wb"
LANZOU_PWD = "1jjn"
CONFIG_FILE_NAME = ".officegitsync.json"
SINGLE_INSTANCE_PORT = 65432  # 单实例通信端口

# 默认排除规则（含 Office/WPS/FreeFileSync 常见临时文件）
OFFICE_WPS_FFS_PRESETS = [
    "~$*",        # Office/WPS 临时锁文件
    "*.tmp",      # 通用/WPS/Office 临时文件
    "*.bak",      # 自动备份文件
    "*.ffs_tmp",  # FreeFileSync 传输临时文件
    "*.ffs_lock", # FreeFileSync 目录锁文件
    "*.ffs_db",   # FreeFileSync 同步数据库文件
    "*.crdownload"
]

CORE_EXCLUDE_PATTERNS = [
    ".git/*",     # Git 核心目录
]

DEFAULT_EXCLUDE_PATTERNS = OFFICE_WPS_FFS_PRESETS + CORE_EXCLUDE_PATTERNS


def _decode_path(path):
    """将 Dulwich 返回的路径统一为 Git 使用的相对 POSIX 路径。"""
    if isinstance(path, bytes):
        path = path.decode("utf-8", errors="replace")
    normalized = str(path).replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized


def _status_paths(status):
    """提取 Git 状态中的所有路径，兼容 Dulwich 不同版本的状态结构。"""
    staged = getattr(status, "staged", {}) or {}
    paths = set()
    for values in staged.values():
        for path in values or []:
            paths.add(_decode_path(path))

    for attr in ("unstaged", "untracked"):
        values = getattr(status, attr, []) or []
        if isinstance(values, dict):
            iterable = []
            for group in values.values():
                iterable.extend(group or [])
        else:
            iterable = values
        for path in iterable:
            paths.add(_decode_path(path))
    return paths


def get_changed_files(repo_dir, enable_log=False, exclude_patterns=None, candidate_paths=None):
    """返回 Git 确认存在变更的文件；若给出候选路径，只返回本轮文件事件涉及的路径。"""
    try:
        status = porcelain.status(repo_dir)
        found = _status_paths(status)

        if candidate_paths is not None:
            candidates = {_decode_path(path) for path in candidate_paths if path}
            found.intersection_update(candidates)

        patterns = list(CORE_EXCLUDE_PATTERNS)
        if exclude_patterns:
            patterns.extend(exclude_patterns)

        result = []
        for rel_path in sorted(found, key=str.lower):
            normalized = _decode_path(rel_path)
            filename = os.path.basename(normalized)
            if normalized == ".git" or normalized.startswith(".git/"):
                continue
            if any(fnmatch.fnmatch(filename, pat) or fnmatch.fnmatch(normalized, pat) for pat in patterns):
                continue
            result.append(normalized)
        return result
    except Exception as e:
        log_message(f"读取 Git 变更状态失败: {e}", enable_log)
        return []


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
            "enable_log": False,
            "silent_start": False
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
    """防抖监听器：只把本轮真实文件事件对应的路径交给 Git 状态检查。"""
    def __init__(self, repo_dir, debounce_seconds=3, exclude_patterns=None, trigger_callback=None):
        super().__init__()
        self.repo_dir = os.path.abspath(repo_dir)
        self.debounce_seconds = debounce_seconds
        self.exclude_patterns = exclude_patterns if exclude_patterns is not None else list(DEFAULT_EXCLUDE_PATTERNS)
        self.trigger_callback = trigger_callback
        self.timer = None
        self.lock = threading.Lock()
        self.pending_paths = set()

    def is_excluded(self, path):
        try:
            rel_path = os.path.relpath(os.path.abspath(path), self.repo_dir).replace("\\", "/")
        except ValueError:
            return True
        filename = os.path.basename(path)
        if rel_path == ".git" or rel_path.startswith(".git/") or rel_path.startswith("../"):
            return True
        for pattern in self.exclude_patterns:
            if fnmatch.fnmatch(filename, pattern) or fnmatch.fnmatch(rel_path, pattern):
                return True
        return False

    def on_any_event(self, event):
        if event.is_directory:
            return

        event_paths = [getattr(event, "src_path", None)]
        dest_path = getattr(event, "dest_path", None)
        if dest_path:
            event_paths.append(dest_path)

        paths = set()
        for path in event_paths:
            if not path or self.is_excluded(path):
                continue
            try:
                rel_path = os.path.relpath(os.path.abspath(path), self.repo_dir).replace("\\", "/")
            except ValueError:
                continue
            if rel_path and rel_path != "." and not rel_path.startswith("../"):
                paths.add(_decode_path(rel_path))
        if not paths:
            return

        with self.lock:
            self.pending_paths.update(paths)
            if self.timer:
                self.timer.cancel()
            self.timer = threading.Timer(self.debounce_seconds, self.on_debounce_success)
            self.timer.daemon = True
            self.timer.start()

    def on_debounce_success(self):
        with self.lock:
            paths = set(self.pending_paths)
            self.pending_paths.clear()
            self.timer = None
        if paths and self.trigger_callback:
            self.trigger_callback(paths)


def _ensure_timeline_ref(repo):
    """
    确保仓库中存在专用的全量时间线分支 refs/heads/officegitsync-timeline。
    该引用始终指向最新提交（时间线顶端），从而保证：
    1. 恢复到任意旧版本后，较新版本依然保留在全量历史中，不会变成悬空提交或消失；
    2. 恢复到任意版本后，依然可以随时再前进恢复到更新的版本，或再次回溯到更旧的版本；
    3. 仅当用户手动删除某版本时，才会从时间线历史中精准剔除该提交节点。
    """
    timeline_ref = b"refs/heads/officegitsync-timeline"
    if timeline_ref in repo.refs:
        return repo.refs[timeline_ref]
    try:
        head_sha = repo.head()
        repo.refs[timeline_ref] = head_sha
        return head_sha
    except KeyError:
        return None


def _rebuild_commit_chain_without(repo, tip_sha, target_sha):
    """
    在保留其余所有提交快照（tree、提交时间、提交说明、作者）的前提下，
    从时间线中精准移除 target_sha 对应的提交节点，并重建后续节点的父指针链。
    同时自动迁移指向被重建提交的标签（refs/tags/*）。
    """
    walker = repo.get_walker(include=[tip_sha])
    commits = [entry.commit for entry in walker]  # 新 -> 旧
    commits.reverse()  # 旧 -> 新

    if not any(c.id == target_sha for c in commits):
        return tip_sha, {}

    sha_map = {}
    new_parent = None
    for old_commit in commits:
        if old_commit.id == target_sha:
            # 跳过目标节点，保持 new_parent 不变，使下一个节点直接衔接前驱节点
            continue

        new_parents = [new_parent] if new_parent is not None else []
        # 若父节点未变化且当前节点也无需重写，直接复用原对象
        if old_commit.parents == new_parents:
            sha_map[old_commit.id] = old_commit.id
            new_parent = old_commit.id
            continue

        nc = Commit()
        nc.tree = old_commit.tree
        nc.parents = new_parents
        nc.author = old_commit.author
        nc.committer = old_commit.committer
        nc.commit_time = old_commit.commit_time
        nc.commit_timezone = old_commit.commit_timezone
        nc.author_time = old_commit.author_time
        nc.author_timezone = old_commit.author_timezone
        nc.message = old_commit.message
        if old_commit.encoding:
            nc.encoding = old_commit.encoding
        repo.object_store.add_object(nc)
        sha_map[old_commit.id] = nc.id
        new_parent = nc.id

    # 更新所有指向旧 SHA 的标签（支持轻量标签与附注标签）
    for ref_name in list(repo.refs.keys()):
        if not ref_name.startswith(b"refs/tags/"):
            continue
        ref_target = repo.refs[ref_name]
        if ref_target == target_sha:
            del repo.refs[ref_name]
            continue
        if ref_target in sha_map and sha_map[ref_target] != ref_target:
            repo.refs[ref_name] = sha_map[ref_target]
            continue
        try:
            obj = repo[ref_target]
            if isinstance(obj, Tag):
                target_obj_sha = obj.object[1]
                if target_obj_sha == target_sha:
                    del repo.refs[ref_name]
                elif target_obj_sha in sha_map and sha_map[target_obj_sha] != target_obj_sha:
                    new_tag = Tag()
                    new_tag.name = obj.name
                    new_tag.message = obj.message
                    new_tag.tagger = obj.tagger
                    new_tag.tag_time = obj.tag_time
                    new_tag.tag_timezone = obj.tag_timezone
                    new_tag.object = (Commit, sha_map[target_obj_sha])
                    repo.object_store.add_object(new_tag)
                    repo.refs[ref_name] = new_tag.id
        except Exception:
            pass

    return new_parent, sha_map


class CommitDialog(tk.Toplevel):
    """文件变更自动弹出或手动触发的 Commit / 版本标签对话框"""
    def __init__(self, parent, repo_dir, changed_files, enable_log=False, manual_mode=False):
        super().__init__(parent)
        self.title("手动创建备份版本 / 标签" if manual_mode else "检测到文件变更 - 备份确认")
        self.geometry("540x480")
        self.repo_dir = repo_dir
        self.changed_files = changed_files
        self.enable_log = enable_log
        self.manual_mode = manual_mode

        self.attributes("-topmost", True)
        self.focus_force()

        header_text = (
            "当前工作区检测到以下变更文件（可直接提交或附带版本标签）："
            if manual_mode
            else "以下文件发生了修改/更新，请确认备份："
        )
        ttk.Label(self, text=header_text, font=("Microsoft YaHei", 9, "bold")).pack(anchor="w", padx=10, pady=8)

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
        prefix = "Manual-backup" if manual_mode else "Auto-backup"
        self.entry_msg.insert(0, f"{prefix}: {time.strftime('%Y-%m-%d %H:%M:%S')}")

        ttk.Label(self, text="可选版本标签 (Tag，例如 v1.0，留空则不创建标签):").pack(anchor="w", padx=10, pady=(5, 0))
        self.entry_tag = ttk.Entry(self)
        self.entry_tag.pack(fill="x", padx=10, pady=5)

        btn_frame = ttk.Frame(self)
        btn_frame.pack(fill="x", padx=10, pady=10)

        ttk.Button(btn_frame, text="提交备份", command=self.do_commit).pack(side="right", padx=5)
        ttk.Button(btn_frame, text="暂不提交", command=self.destroy).pack(side="right")

    def do_commit(self):
        msg = self.entry_msg.get().strip()
        tag_name = self.entry_tag.get().strip()
        if not msg:
            messagebox.showwarning("提示", "请输入备份说明！", parent=self)
            return
        try:
            selected = {_decode_path(path) for path in self.changed_files}
            current_files = get_changed_files(
                self.repo_dir, self.enable_log, candidate_paths=selected
            )
            paths = [path for path in current_files if path in selected]
            if not paths:
                messagebox.showinfo("无需提交", "这些文件目前没有 Git 变更，无需提交。", parent=self)
                self.destroy()
                return

            before_status = porcelain.status(self.repo_dir)
            staged = getattr(before_status, "staged", {}) or {}
            pre_staged_paths = set()
            for values in staged.values():
                for path in values or []:
                    pre_staged_paths.add(_decode_path(path))
            unrelated_staged = sorted(pre_staged_paths - selected)
            if unrelated_staged:
                preview = "\n".join(unrelated_staged[:8])
                if len(unrelated_staged) > 8:
                    preview += f"\n……另有 {len(unrelated_staged) - 8} 个文件"
                messagebox.showwarning(
                    "发现其他已暂存文件",
                    "为避免意外提交不属于本次备份的内容，已取消提交。\n\n"
                    "请先在 Git 客户端处理这些已暂存文件，再重试：\n" + preview,
                    parent=self,
                )
                return

            existing_paths = [
                path for path in paths
                if os.path.lexists(os.path.join(self.repo_dir, path.replace("/", os.sep)))
            ]
            deleted_paths = [path for path in paths if path not in existing_paths]

            if existing_paths:
                porcelain.add(self.repo_dir, existing_paths)
            if deleted_paths:
                porcelain.rm(self.repo_dir, deleted_paths, cached=True)

            after_status = porcelain.status(self.repo_dir)
            staged_after = getattr(after_status, "staged", {}) or {}
            staged_paths_after = set()
            for values in staged_after.values():
                for path in values or []:
                    staged_paths_after.add(_decode_path(path))

            unrelated_after = sorted(staged_paths_after - selected)
            if unrelated_after:
                raise RuntimeError(
                    "暂存区出现不属于本次备份的文件，已停止提交以保护其他内容："
                    + ", ".join(unrelated_after[:8])
                )
            if not staged_paths_after:
                messagebox.showinfo("无需提交", "没有可提交的已暂存变更。", parent=self)
                self.destroy()
                return

            repo = Repo(self.repo_dir)
            _ensure_timeline_ref(repo)

            commit_sha = porcelain.commit(
                self.repo_dir,
                message=msg.encode("utf-8"),
                author=f"{APP_NAME} <backup@local>".encode("utf-8"),
            )
            if isinstance(commit_sha, str):
                commit_sha = commit_sha.encode("utf-8")

            # 同步更新全量时间线分支指针到最新提交
            repo.refs[b"refs/heads/officegitsync-timeline"] = commit_sha

            if tag_name:
                tag_ref = f"refs/tags/{tag_name}".encode("utf-8")
                repo.refs[tag_ref] = commit_sha

            log_message(f"成功提交 Commit: {msg}" + (f" [Tag: {tag_name}]" if tag_name else ""), self.enable_log)
            messagebox.showinfo("成功", "工作文档已完成备份提交！", parent=self)
            self.destroy()
        except Exception as e:
            show_error("提交失败", "执行 Git Commit 时发生错误", str(e), self.enable_log)


class HistoryWindow(tk.Toplevel):
    """
    历史版本管理窗口：
    - 可恢复到任意版本（包括比当前更新或更旧的版本），且恢复后所有其他版本均完整保留、互不影响；
    - 可手动删除任意版本，删除后其余所有版本（无论在其之前或之后）均不受影响且仍可随时恢复；
    - 支持为任意历史版本创建/附加标签。
    """
    def __init__(self, parent, repo_dir, enable_log=False):
        super().__init__(parent)
        self.title("历史版本管理与回溯")
        self.geometry("740x460")
        self.repo_dir = repo_dir
        self.enable_log = enable_log

        info_lbl = ttk.Label(
            self,
            text="提示：恢复到任意版本不会删除其前后的其他版本；删除任意版本也不会影响其余版本的完整恢复。",
            foreground="#444444"
        )
        info_lbl.pack(anchor="w", padx=10, pady=(8, 2))

        self.tree = ttk.Treeview(self, columns=("SHA", "Time", "Tags", "Message"), show="headings")
        self.tree.heading("SHA", text="Commit SHA")
        self.tree.heading("Time", text="备份时间")
        self.tree.heading("Tags", text="版本标签")
        self.tree.heading("Message", text="提交说明")
        self.tree.column("SHA", width=95)
        self.tree.column("Time", width=145)
        self.tree.column("Tags", width=120)
        self.tree.column("Message", width=340)

        self.tree.pack(fill="both", expand=True, padx=10, pady=8)

        btn_frame = ttk.Frame(self)
        btn_frame.pack(fill="x", padx=10, pady=(0, 10))

        ttk.Button(btn_frame, text="恢复到此版本", command=self.restore_version).pack(side="left", padx=5)
        ttk.Button(btn_frame, text="删除选中版本", command=self.delete_version).pack(side="left", padx=5)
        ttk.Button(btn_frame, text="为选中版本打标签", command=self.tag_selected_version).pack(side="left", padx=5)
        ttk.Button(btn_frame, text="刷新列表", command=self.load_history).pack(side="right", padx=5)

        self.load_history()

    def _collect_tags_map(self, repo):
        tags_map = {}
        for ref_name in repo.refs.keys():
            if not ref_name.startswith(b"refs/tags/"):
                continue
            tag_label = ref_name[len(b"refs/tags/"):].decode("utf-8", errors="replace")
            target = repo.refs[ref_name]
            try:
                obj = repo[target]
                if isinstance(obj, Tag):
                    target = obj.object[1]
            except Exception:
                pass
            sha_str = target.decode("utf-8") if isinstance(target, bytes) else str(target)
            tags_map.setdefault(sha_str, []).append(tag_label)
        return tags_map

    def load_history(self):
        for item in self.tree.get_children():
            self.tree.delete(item)
        try:
            repo = Repo(self.repo_dir)
            timeline_sha = _ensure_timeline_ref(repo)
            try:
                head_sha = repo.head()
            except KeyError:
                head_sha = None

            if not timeline_sha and not head_sha:
                return

            include_refs = []
            if timeline_sha:
                include_refs.append(timeline_sha)
            if head_sha and head_sha not in include_refs:
                include_refs.append(head_sha)

            tags_map = self._collect_tags_map(repo)
            walker = repo.get_walker(include=include_refs)
            for entry in walker:
                commit = entry.commit
                full_sha = commit.id.decode('utf-8') if isinstance(commit.id, bytes) else str(commit.id)
                short_sha = full_sha[:7]
                if head_sha and commit.id == head_sha:
                    short_sha = f"{short_sha} (当前)"

                raw_msg = commit.message
                msg = raw_msg.decode('utf-8', errors='ignore').strip() if isinstance(raw_msg, bytes) else str(raw_msg).strip()

                ctime = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(commit.commit_time))
                tag_str = ", ".join(tags_map.get(full_sha, []))
                self.tree.insert("", "end", iid=full_sha, values=(short_sha, ctime, tag_str, msg))
        except Exception as e:
            show_error("加载历史失败", "无法读取 Git 提交历史记录", str(e), self.enable_log)

    def restore_version(self):
        selected = self.tree.selection()
        if not selected:
            messagebox.showwarning("提示", "请先选择要恢复的历史版本！", parent=self)
            return

        target_sha = selected[0]
        if messagebox.askyesno(
            "确认恢复",
            f"是否将工作区文件恢复到版本 [{target_sha[:7]}]？\n\n"
            "说明：\n"
            "1. 该版本之前和之后的所有历史版本都会完整保留，不会被删除；\n"
            "2. 恢复后，您仍可随时切换到其他任意更旧或更新的版本；\n"
            "3. 当前工作区中尚未提交的改动将被该版本的文件覆盖。",
            parent=self,
        ):
            try:
                repo = Repo(self.repo_dir)
                # 先确保全量时间线指针指向最新的历史顶端，防止回溯后丢失较新提交
                timeline_sha = _ensure_timeline_ref(repo)
                try:
                    cur_head = repo.head()
                except KeyError:
                    cur_head = None

                if cur_head and timeline_sha:
                    # 如果当前 HEAD 位于时间线之外的更新提交，更新 timeline_sha
                    walker = repo.get_walker(include=[cur_head])
                    cur_ancestors = {e.commit.id for e in walker}
                    if timeline_sha in cur_ancestors and cur_head != timeline_sha:
                        repo.refs[b"refs/heads/officegitsync-timeline"] = cur_head

                porcelain.reset(self.repo_dir, "hard", target_sha.encode("utf-8"))
                log_message(f"已恢复工作区到版本 [{target_sha[:7]}]（全量历史已保留）", self.enable_log)
                messagebox.showinfo("成功", f"工作区已成功恢复到版本 [{target_sha[:7]}]！\n其余历史版本均已完整保留。", parent=self)
                self.load_history()
            except Exception as e:
                show_error("还原失败", "恢复指定版本时出错", str(e), self.enable_log)

    def delete_version(self):
        selected = self.tree.selection()
        if not selected:
            messagebox.showwarning("提示", "请先选择要删除的历史版本！", parent=self)
            return

        target_sha = selected[0]
        all_items = self.tree.get_children()
        if len(all_items) <= 1:
            messagebox.showwarning(
                "无法删除",
                "当前仅剩最后一个历史版本。为防止仓库失去全部基线，不支持删除唯一的提交记录。",
                parent=self,
            )
            return

        if not messagebox.askyesno(
            "确认删除版本",
            f"确定要手动删除历史版本 [{target_sha[:7]}] 吗？\n\n"
            "说明：\n"
            "• 仅移除该版本的提交记录；\n"
            "• 该版本之前和之后的所有其他版本均完整保留，且仍可随时恢复。",
            parent=self,
        ):
            return

        try:
            repo = Repo(self.repo_dir)
            timeline_sha = _ensure_timeline_ref(repo)
            try:
                cur_head = repo.head()
            except KeyError:
                cur_head = timeline_sha

            target_bytes = target_sha.encode("utf-8")
            tip_sha = timeline_sha or cur_head
            new_tip, sha_map = _rebuild_commit_chain_without(repo, tip_sha, target_bytes)

            if new_tip is None:
                raise RuntimeError("未能生成有效的新提交链，操作已中止。")

            # 更新全量时间线分支
            repo.refs[b"refs/heads/officegitsync-timeline"] = new_tip

            # 同步更新当前活动分支（如 refs/heads/master 或 main）
            head_symref = repo.refs.follow(b"HEAD")[0]
            active_branch = None
            for ref_chain in head_symref:
                if ref_chain.startswith(b"refs/heads/"):
                    active_branch = ref_chain
                    break

            if cur_head == target_bytes:
                # 若删除的恰好是当前检出的版本，将当前指针平滑切换至相邻保留版本（不覆盖用户未改动的工作区）
                walker = repo.get_walker(include=[tip_sha])
                ordered = [e.commit.id for e in walker]  # 新 -> 旧
                idx = ordered.index(target_bytes)
                fallback_old = ordered[idx + 1] if idx + 1 < len(ordered) else ordered[idx - 1]
                new_head_sha = sha_map.get(fallback_old, new_tip)
                if active_branch:
                    repo.refs[active_branch] = new_head_sha
                else:
                    repo.refs[b"HEAD"] = new_head_sha
                # 仅重置 index 以匹配新基线，保留工作区现有文件不被意外覆盖
                porcelain.reset(self.repo_dir, "mixed", new_head_sha)
            else:
                new_head_sha = sha_map.get(cur_head, new_tip)
                if active_branch:
                    repo.refs[active_branch] = new_head_sha
                else:
                    repo.refs[b"HEAD"] = new_head_sha
                porcelain.reset(self.repo_dir, "mixed", new_head_sha)

            log_message(f"已手动删除版本 [{target_sha[:7]}]，其余所有版本均已保留", self.enable_log)
            messagebox.showinfo("删除成功", f"已删除版本 [{target_sha[:7]}]！\n其他所有历史版本均未受影响，仍可随时恢复。", parent=self)
            self.load_history()
        except Exception as e:
            show_error("删除失败", "删除指定历史版本时发生错误", str(e), self.enable_log)

    def tag_selected_version(self):
        selected = self.tree.selection()
        if not selected:
            messagebox.showwarning("提示", "请先选择要添加标签的历史版本！", parent=self)
            return
        target_sha = selected[0]
        tag_name = simpledialog.askstring("创建版本标签", f"请输入版本 [{target_sha[:7]}] 的标签名称（如 v1.0）：", parent=self)
        if not tag_name or not tag_name.strip():
            return
        tag_name = tag_name.strip()
        try:
            repo = Repo(self.repo_dir)
            tag_ref = f"refs/tags/{tag_name}".encode("utf-8")
            repo.refs[tag_ref] = target_sha.encode("utf-8")
            log_message(f"已为版本 [{target_sha[:7]}] 创建标签: {tag_name}", self.enable_log)
            self.load_history()
        except Exception as e:
            show_error("创建标签失败", "写入 Git 标签时出错", str(e), self.enable_log)


class BackupApp:
    def __init__(self, root):
        self.root = root
        self.root.title(f"{APP_NAME} v{APP_VERSION}")
        self.root.geometry("680x630")

        self.observer = None
        self.watch_dir = ""
        self.exclude_patterns = list(DEFAULT_EXCLUDE_PATTERNS)
        self.enable_log = False
        self.silent_start = False
        self.dialog_lock = False

        self.load_app_config()
        self.setup_ui()
        self.create_tray_icon()
        self.check_autostart_status()

        # 若启用了后台静默启动，且已配置有效工作目录，自动隐藏主窗体并开启后台监控
        if self.silent_start:
            self.root.after(100, self._apply_silent_startup)

    def _apply_silent_startup(self):
        self.hide_window()
        if self.watch_dir and os.path.exists(self.watch_dir):
            self.start_monitoring(silent=True)

    def load_app_config(self):
        config = AppConfigManager.load_config()
        self.watch_dir = config.get("watch_dir", "")
        self.exclude_patterns = config.get("exclude_patterns", list(DEFAULT_EXCLUDE_PATTERNS))
        self.enable_log = config.get("enable_log", False)
        self.silent_start = config.get("silent_start", False)

    def save_app_config(self):
        config_data = {
            "version": APP_VERSION,
            "watch_dir": self.watch_dir,
            "exclude_patterns": self.exclude_patterns,
            "enable_log": self.enable_log,
            "silent_start": self.silent_start
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
        has_presets = any(p in self.exclude_patterns for p in OFFICE_WPS_FFS_PRESETS)
        self.var_office_lock = tk.BooleanVar(value=has_presets)
        ttk.Checkbutton(
            preset_frame,
            text="排除 Office / WPS / FreeFileSync 临时文件 (~$*, *.tmp, *.bak, *.ffs_tmp, *.ffs_lock, *.ffs_db)",
            variable=self.var_office_lock,
            command=self.toggle_preset_rules
        ).pack(side="left")

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
        ttk.Checkbutton(opt_frame, text="开机自启动", variable=self.var_autostart, command=self.toggle_autostart).pack(side="left", padx=8)

        self.var_silent = tk.BooleanVar(value=self.silent_start)
        ttk.Checkbutton(opt_frame, text="启动时后台静默运行", variable=self.var_silent, command=self.toggle_silent_start).pack(side="left", padx=8)

        self.var_log = tk.BooleanVar(value=self.enable_log)
        ttk.Checkbutton(opt_frame, text="输出 Log 日志 (默认关闭)", variable=self.var_log, command=self.toggle_log).pack(side="left", padx=8)

        btn_frame = ttk.Frame(self.root, padding=5)
        btn_frame.pack(fill="x", padx=10)

        self.btn_toggle = ttk.Button(btn_frame, text="开启无感自动备份", command=self.toggle_monitoring)
        self.btn_toggle.pack(side="left", fill="x", expand=True, padx=4)

        ttk.Button(btn_frame, text="手动创建备份/标签", command=self.manual_commit_dialog).pack(side="left", padx=4)
        ttk.Button(btn_frame, text="历史版本管理", command=self.open_history_window).pack(side="right", padx=4)

        self.lbl_status = ttk.Label(self.root, text="状态: 未启动", foreground="gray")
        self.lbl_status.pack(pady=3)

        footer_frame = ttk.Frame(self.root, padding=6)
        footer_frame.pack(side="bottom", fill="x")

        author_lbl = ttk.Label(footer_frame, text=f"Author: {APP_AUTHOR} | v{APP_VERSION}", font=("Microsoft YaHei", 9))
        author_lbl.pack(side="left", padx=8)

        links_frame = ttk.Frame(footer_frame)
        links_frame.pack(side="right", padx=8)

        github_lbl = ttk.Label(links_frame, text="GitHub开源地址", foreground="blue", cursor="hand2", font=("Microsoft YaHei", 9, "underline"))
        github_lbl.pack(side="left", padx=6)
        github_lbl.bind("<Button-1>", lambda e: webbrowser.open(APP_URL))

        lanzou_lbl = ttk.Label(links_frame, text=f"蓝奏云下载 (密码:{LANZOU_PWD})", foreground="blue", cursor="hand2", font=("Microsoft YaHei", 9, "underline"))
        lanzou_lbl.pack(side="left", padx=6)
        lanzou_lbl.bind("<Button-1>", lambda e: webbrowser.open(LANZOU_URL))

        self.update_listbox()

    def update_listbox(self):
        self.pattern_listbox.delete(0, tk.END)
        for pattern in self.exclude_patterns:
            self.pattern_listbox.insert(tk.END, pattern)

    def check_and_init_repo(self, target_dir, ask_import=True):
        """
        检查目标目录是否已有 .git 目录：
        - 若已存在 .git 目录且 ask_import=True，弹窗提示用户是否直接导入该已有 Git 项目；
        - 若不存在 .git 目录，自动初始化新建项目。
        """
        git_dir = os.path.join(target_dir, ".git")
        if os.path.exists(git_dir):
            if ask_import:
                confirm = messagebox.askyesno(
                    "检测到已有 Git 仓库",
                    f"目录 [{os.path.basename(target_dir)}] 中已存在 .git 版本库。\n\n"
                    "是否直接导入并接管该已有项目的历史版本进行监控备份？"
                )
                if not confirm:
                    return False
            try:
                repo = Repo(target_dir)
                _ensure_timeline_ref(repo)
                return True
            except Exception as e:
                show_error("导入仓库失败", "无法读取已存在的 Git 版本库；原有仓库未被修改。", str(e), self.enable_log)
                return False
        try:
            Repo.init(target_dir)
            log_message(f"已在目录初始化新建 Git 项目: {target_dir}", self.enable_log)
            return True
        except Exception as e:
            show_error("初始化仓库失败", "无法创建 Git 版本库", str(e), self.enable_log)
            return False

    def browse_directory(self):
        selected_dir = filedialog.askdirectory()
        if selected_dir:
            if self.check_and_init_repo(selected_dir, ask_import=True):
                self.watch_dir = selected_dir
                self.dir_entry.delete(0, tk.END)
                self.dir_entry.insert(0, selected_dir)
                self.save_app_config()

    def toggle_preset_rules(self):
        if self.var_office_lock.get():
            for p in OFFICE_WPS_FFS_PRESETS:
                if p not in self.exclude_patterns:
                    self.exclude_patterns.append(p)
        else:
            self.exclude_patterns = [p for p in self.exclude_patterns if p not in OFFICE_WPS_FFS_PRESETS]
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

    def toggle_silent_start(self):
        self.silent_start = self.var_silent.get()
        self.save_app_config()

    def _autostart_command(self):
        if getattr(sys, "frozen", False):
            return f'"{os.path.abspath(sys.executable)}"'
        script_path = os.path.abspath(__file__)
        python_path = os.path.abspath(sys.executable)
        base_dir = os.path.dirname(python_path)
        pythonw = os.path.join(base_dir, "pythonw.exe")
        launcher = pythonw if os.path.isfile(pythonw) else python_path
        return f'"{launcher}" "{script_path}"'

    def check_autostart_status(self):
        key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_READ) as key:
                command, _ = winreg.QueryValueEx(key, APP_NAME)
            expected = self._autostart_command()

            if command.strip() != expected.strip():
                with winreg.OpenKey(
                    winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE
                ) as write_key:
                    winreg.SetValueEx(write_key, APP_NAME, 0, winreg.REG_SZ, expected)
                log_message(f"已修正旧的开机启动命令: {expected}", self.enable_log)
            self.var_autostart.set(True)
            log_message(f"检测到开机启动项: {expected}", self.enable_log)
        except FileNotFoundError:
            self.var_autostart.set(False)
        except OSError as e:
            self.var_autostart.set(False)
            log_message(f"读取/修正开机启动项失败: {e}", self.enable_log)

    def toggle_autostart(self):
        key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
        try:
            with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE | winreg.KEY_QUERY_VALUE) as key:
                if self.var_autostart.get():
                    command = self._autostart_command()
                    winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, command)
                    saved_command, _ = winreg.QueryValueEx(key, APP_NAME)
                    if saved_command != command:
                        raise OSError("注册表中的启动命令与写入内容不一致。")
                    log_message(f"已设置开机自启动: {command}", self.enable_log)
                else:
                    try:
                        winreg.DeleteValue(key, APP_NAME)
                    except FileNotFoundError:
                        pass
                    log_message("已关闭开机自启动", self.enable_log)
        except Exception as e:
            self.var_autostart.set(not self.var_autostart.get())
            show_error("设置失败", "无法修改或验证注册表开机自启项", str(e), self.enable_log)

    def on_file_changed(self, candidate_paths=None):
        if self.dialog_lock or not self.watch_dir:
            return
        candidate_paths = set(candidate_paths or [])
        if not candidate_paths:
            return

        def show_dialog():
            if self.dialog_lock:
                return
            self.dialog_lock = True
            try:
                changed_files = get_changed_files(
                    self.watch_dir,
                    self.enable_log,
                    self.exclude_patterns,
                    candidate_paths=candidate_paths,
                )
                if not changed_files:
                    return
                dlg = CommitDialog(self.root, self.watch_dir, changed_files, self.enable_log, manual_mode=False)
                self.root.wait_window(dlg)
            except Exception as e:
                show_error("文件变动处理失败", "检查 Git 文件变更时发生异常", str(e), self.enable_log)
            finally:
                self.dialog_lock = False

        self.root.after(0, show_dialog)

    def manual_commit_dialog(self):
        target_dir = self.dir_entry.get().strip()
        if not target_dir or not os.path.exists(target_dir):
            messagebox.showwarning("提示", "请先选择有效的工作备份目录！")
            return
        if not self.check_and_init_repo(target_dir, ask_import=(target_dir != self.watch_dir)):
            return
        self.watch_dir = target_dir
        self.save_app_config()

        if self.dialog_lock:
            return
        self.dialog_lock = True
        try:
            changed_files = get_changed_files(self.watch_dir, self.enable_log, self.exclude_patterns)
            if not changed_files:
                messagebox.showinfo("提示", "当前工作目录中没有未提交的文件变更。")
                return
            dlg = CommitDialog(self.root, self.watch_dir, changed_files, self.enable_log, manual_mode=True)
            self.root.wait_window(dlg)
        except Exception as e:
            show_error("手动备份失败", "扫描工作区变更时发生错误", str(e), self.enable_log)
        finally:
            self.dialog_lock = False

    def toggle_monitoring(self):
        if self.observer and self.observer.is_alive():
            self.stop_monitoring()
        else:
            self.start_monitoring(silent=False)

    def start_monitoring(self, silent=False):
        input_dir = self.dir_entry.get().strip()
        if not input_dir or not os.path.exists(input_dir):
            if not silent:
                messagebox.showerror("错误", "请先选择有效的工作备份目录！")
            return

        need_prompt = (not silent) and (input_dir != self.watch_dir)
        if not self.check_and_init_repo(input_dir, ask_import=need_prompt):
            return
        self.watch_dir = input_dir

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
        target_dir = self.dir_entry.get().strip() or self.watch_dir
        if not target_dir or not os.path.exists(target_dir):
            messagebox.showwarning("提示", "请先选择有效的备份工作目录！")
            return
        self.watch_dir = target_dir
        HistoryWindow(self.root, self.watch_dir, self.enable_log)

    def create_tray_icon(self):
        image = Image.new('RGB', (64, 64), color=(0, 120, 215))
        draw = ImageDraw.Draw(image)
        draw.rectangle([16, 16, 48, 48], fill=(255, 255, 255))

        menu = pystray.Menu(
            pystray.MenuItem("显示主界面", self.show_window),
            pystray.MenuItem("退出程序", self.quit_app)
        )
        self.tray_icon = pystray.Icon(APP_NAME, image, f"{APP_NAME} v{APP_VERSION}", menu)
        threading.Thread(target=self.tray_icon.run, daemon=True).start()
        self.root.protocol('WM_DELETE_WINDOW', self.hide_window)

    def hide_window(self):
        self.root.withdraw()

    def show_window(self, icon=None, item=None):
        self.root.after(0, lambda: wake_up_window(self.root))

    def quit_app(self, icon=None, item=None):
        self.stop_monitoring()
        if self.tray_icon:
            self.tray_icon.stop()
        self.root.after(0, self.root.destroy)


# 保留一个全局引用，防止监听 socket 被 Python 垃圾回收提前关闭
_instance_server_sock = None


def wake_up_window(root):
    """确保窗口从最小化、隐藏或托盘状态还原并强行置顶激活"""
    root.deiconify()
    root.state('normal')
    root.lift()
    root.attributes("-topmost", True)
    root.after_idle(root.attributes, "-topmost", False)
    root.focus_force()


def ensure_single_instance(root):
    global _instance_server_sock

    client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        client.settimeout(0.5)
        client.connect(("127.0.0.1", SINGLE_INSTANCE_PORT))
        try:
            client.sendall(b"WAKE_UP\n")
        except Exception:
            pass
        finally:
            client.close()
        return False
    except (OSError, socket.error):
        client.close()

    _instance_server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        _instance_server_sock.bind(("127.0.0.1", SINGLE_INSTANCE_PORT))
        _instance_server_sock.listen(5)

        def listen_wake_up():
            while True:
                try:
                    conn, _ = _instance_server_sock.accept()
                    root.after(0, lambda: wake_up_window(root))
                    conn.close()
                except Exception:
                    break

        t = threading.Thread(target=listen_wake_up, daemon=True)
        t.start()
        return True
    except OSError as e:
        try:
            _instance_server_sock.close()
        except Exception:
            pass
        _instance_server_sock = None
        log_message(f"单实例端口 {SINGLE_INSTANCE_PORT} 绑定失败: {e}")
        return True


if __name__ == "__main__":
    root = tk.Tk()
    if not ensure_single_instance(root):
        root.destroy()
        sys.exit(0)
    app = BackupApp(root)
    root.mainloop()
