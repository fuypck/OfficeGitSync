# OfficeGitSync 📄⚡

**OfficeGitSync v0.1.5** 是一款面向 Windows 的轻量级、本地 Git 文档版本备份工具。它使用 Watchdog 监听文件系统变化，通过 Dulwich 管理 Git 版本历史，主要面向 WPS、Microsoft Office、设计文件及其他日常工作文件。

> **开发状态：v0.1.5（待实机验证）**。目前功能已在源码中实现，但尚未完成真实 Windows 环境下的系统测试，尤其是自动备份、开机自启动、历史恢复与删除。请勿将本程序作为重要文件的唯一备份手段。

## 主要功能

- **实时监听与防抖**：监听指定目录及子目录的文件事件，默认将连续事件合并到约 3 秒后处理。
- **精确识别变更**：结合本轮文件事件与 Git 状态，仅提示相应的实际变更路径，减少无关文件的重复弹窗。
- **自定义排除规则**：默认过滤 Office/WPS 锁文件、`*.tmp`、`*.bak`、FreeFileSync 临时文件、`*.crdownload` 及 `.git` 内部文件；可在界面中添加或删除规则。
- **确认后提交备份**：弹窗列出待提交文件，可填写提交说明及可选版本标签；只有确认后才执行 Git 提交，并检查是否存在不属于本次范围的已暂存文件。
- **手动创建备份/标签**：不依赖自动监听事件，也可以主动检查当前工作目录的 Git 变更并创建提交。
- **历史版本管理**：查看提交时间、说明、SHA 与标签，选择历史版本进行恢复，为历史提交添加标签，或删除选中的历史版本。
- **独立时间线引用**：通过 `officegitsync-timeline` 分支尝试保留恢复前后的历史版本，以便在旧版本与较新版本之间切换。
- **已有 Git 仓库导入**：选定目录存在 `.git` 时，支持确认后沿用现有仓库；否则可初始化新仓库。
- **系统托盘与启动选项**：支持关闭主窗口后驻留托盘、后台静默启动，以及通过当前用户注册表配置 Windows 开机自启动；启动时会检查并尝试修正过期的启动路径。
- **单实例机制与可选日志**：尝试阻止重复运行并唤醒已有窗口；可选择在程序目录输出 `officegitsync.log`。

## 快速使用

1. 从 [Releases](https://github.com/fuypck/OfficeGitSync/releases) 下载已发布的 Windows 构建；如果没有可用版本，可检查 [Actions](https://github.com/fuypck/OfficeGitSync/actions) 是否提供构建产物。
2. 运行 `OfficeGitSync.exe`，选择需要监控的工作目录。若已有 Git 仓库，按提示选择是否导入。
3. 点击 **开启无感自动备份**。发生文件变化后，程序会检查 Git 状态并弹出备份确认窗口；点击 **提交备份** 才会创建版本。
4. 使用 **手动创建备份/标签** 主动记录版本，或打开 **历史版本管理** 浏览、恢复、标记及删除历史提交。
5. 按需开启 **开机自启动**、**启动时后台静默运行** 和 **输出 Log 日志**。

**说明：**“无感自动备份”指后台监听与变更检测，并非完全无人值守的自动提交；当前设计仍要求用户确认提交。

## 历史版本与数据安全

v0.1.5 的历史管理不再是只能查看和恢复：源码中已经提供 **删除选中版本** 功能，并通过重建后续提交链实现。此操作会改变部分提交 SHA，也可能影响已有分支、标签或其他 Git 工具的引用关系；**在充分测试前，不建议对重要仓库使用删除功能**。

- **恢复到此版本** 使用 Git 硬重置（`reset --hard`），会覆盖受 Git 管理文件中尚未提交的工作区修改，并改变当前分支指向。操作前请独立复制重要文件。
- **删除选中版本** 会改写 Git 历史。独立时间线引用旨在保留版本可访问性，但不构成在所有分叉、标签和异常情况下的完整性保证。
- Git 历史保存在所监控目录的 `.git` 中；**不会自动上传到 GitHub 或云盘**。请勿随意删除或覆盖 `.git`。
- 建议先用专门创建的测试目录验证恢复、删除和启动行为，确认符合预期后再考虑用于真实文档。

## 从源码运行（Windows）

建议使用 Python 3.10 或更新的兼容版本：

```powershell
git clone https://github.com/fuypck/OfficeGitSync.git
cd OfficeGitSync
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python git_sync.py
```

如果 PowerShell 禁止激活虚拟环境，可直接调用 `.\.venv\Scripts\python.exe` 运行脚本。

## Windows 打包

```powershell
python -m pip install -r requirements.txt pyinstaller
pyinstaller --noconfirm --clean --onedir --windowed --name OfficeGitSync git_sync.py
```

产物位于 `dist/OfficeGitSync/`。请分发整个目录，而不只是单独的 EXE 文件。

## Debian/Linux + Wine 辅助打包

仓库包含 `build_on_debian.sh`，需要预先安装 Wine，并确保 Wine 环境内已有可用的 **Windows Python**：

```bash
wine python --version
bash build_on_debian.sh
```

PyInstaller 不能直接将 Linux Python 程序交叉编译为 Windows 程序。此脚本依赖 Wine 内的 Windows Python；**ARM64 Android/NetHunter 环境未验证兼容性**。

## 自动构建与依赖

如果已配置 GitHub Actions 工作流，可以在 [Actions](https://github.com/fuypck/OfficeGitSync/actions) 查看运行结果；只有工作流上传了 artifact 或发布了 Release 才会出现可下载文件。构建成功也不代表 Windows 实机功能已通过测试。

运行时依赖见 [requirements.txt](requirements.txt)：`dulwich`、`watchdog`、`pystray` 和 `Pillow`；打包另需 `PyInstaller`。

---

## English

**OfficeGitSync v0.1.5** is a lightweight, Windows-oriented local Git version-backup utility for Office documents and other working files. It uses Watchdog for filesystem monitoring and Dulwich for Git operations.

> **Status: Not yet validated on a real Windows system.** The features described below are implemented in the source code but still require practical testing. Do not rely on this application as your only backup.

### Features

- Debounced recursive filesystem monitoring (3-second default) and Git-status-based filtering of event-related changes.
- Configurable exclusion patterns for Office/WPS lock files, temporary files, FreeFileSync files, and Git internals.
- Confirmation dialogs for commits, optional commit tags, and checks against unrelated staged files.
- Manual backups, history browsing, restoring previous versions, adding tags, and deleting selected history entries.
- A dedicated `officegitsync-timeline` ref intended to preserve access to newer versions after restoring older ones.
- Import of existing Git repositories or initialization of new ones.
- System tray, optional silent startup, Windows user-level autostart, single-instance handling, and optional logs.

### Getting started

Download an available build from [Releases](https://github.com/fuypck/OfficeGitSync/releases) or check [Actions](https://github.com/fuypck/OfficeGitSync/actions) for build artifacts. Run `OfficeGitSync.exe`, select a working directory, and start monitoring. **Commits require user confirmation**; monitoring does not silently create commits.

### Important safety notes

- **Restore uses `reset --hard`** and can overwrite uncommitted tracked-file changes and move the current branch pointer.
- **Deleting a history entry rewrites the commit chain** and can change commit IDs or affect other references. This feature has not been validated for all Git history shapes; use a disposable test repository first.
- Git history remains local in `.git`; it is not automatically uploaded to GitHub or any cloud service.
- Keep independent backups of important documents.

### Run from source (Windows)

```powershell
git clone https://github.com/fuypck/OfficeGitSync.git
cd OfficeGitSync
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python git_sync.py
```

### Build on Windows

```powershell
python -m pip install -r requirements.txt pyinstaller
pyinstaller --noconfirm --clean --onedir --windowed --name OfficeGitSync git_sync.py
```

Distribute the complete `dist/OfficeGitSync/` directory.

### Build with Wine on Debian/Linux

`build_on_debian.sh` requires Wine and Windows Python installed inside Wine. It is not a general cross-compilation solution and has not been validated on ARM64 Android/NetHunter.

### CI and dependencies

Check the repository's Actions page for available builds. Successful CI builds are not a substitute for Windows runtime tests. Runtime dependencies are listed in [requirements.txt](requirements.txt); packaging additionally requires PyInstaller.
