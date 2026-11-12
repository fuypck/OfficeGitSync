# OfficeGitSync 📄⚡

**OfficeGitSync** 是一个面向 Windows 的轻量级文件自动备份工具。它使用 Watchdog 监听文件系统事件，并通过 Dulwich 读写 Git 仓库，适用于 WPS、Microsoft Office、设计文件及其他日常工作文件。

> **项目状态：**个人项目。自动备份和历史恢复功能仍需在真实 Windows 环境中测试。请勿把本工具当作唯一备份；重要文件仍应保留独立副本或异地备份。

## 功能

- **文件事件防抖：**合并短时间内连续发生的保存事件。
- **按 Git 状态识别变更：**只提示本轮文件事件涉及且 Git 确认有变更的路径，避免一次事件把整个目录的文件都列为待提交。
- **临时文件过滤：**默认排除 Office/WPS 锁文件、`*.tmp`、`*.bak`、`.git` 内容及 Chrome 下载临时文件；可以在界面中自定义规则。
- **提交前安全检查：**如果 Git 暂存区中有不属于当前备份范围的文件，会停止提交并提示，避免意外把其他暂存内容一起提交。
- **Git 历史浏览与恢复：**可查看提交历史并选择版本恢复。恢复操作会重置当前分支和工作区，可能覆盖未提交的修改或让较新的提交不再显示在当前分支历史中；操作前请先备份。
- **系统托盘与 Windows 开机启动：**支持最小化到托盘，并将启动项指向当前程序路径。

## 使用方法

1. 从 GitHub Releases 下载已发布的 Windows 程序；如果没有可用的 Release，请查看仓库的 **Actions** 页面是否提供构建产物。
2. 启动 `OfficeGitSync.exe`，选择要监控的工作目录。
3. 如果目录已有 `.git`，程序会尝试直接打开并沿用原有仓库，不会主动重新初始化或清空历史。
4. 点击“开启无感自动备份”。文件发生变化后，程序会显示待备份文件和提交说明；确认后才会创建 Git 提交。
5. 可在“历史版本管理”中查看历史。当前版本暂时禁用了直接删除单个历史提交，以避免不安全地改写 Git 提交链。

### 重要提示

- Git 只记录文件版本，不会自动把备份上传到 GitHub 或其他云端。
- 请勿把 `.git` 文件夹删除、移动或覆盖；它包含本地版本历史。
- “恢复到此版本”属于破坏性操作，会重置当前分支和工作区。先复制重要文件，并确认自己理解提示内容。
- 目前应用主要面向 Windows；在其他操作系统上运行或打包不属于已验证的支持场景。

## 从源码运行（Windows）

建议使用 Python 3.10 或更新的兼容版本，并在虚拟环境中安装依赖：

```powershell
git clone https://github.com/fuypck/OfficeGitSync.git
cd OfficeGitSync
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python git_sync.py
```

如果 PowerShell 阻止激活脚本，可以不激活虚拟环境，直接运行 `.\.venv\Scripts\python.exe` 和 `.\.venv\Scripts\pip.exe` 对应的命令。

## 在 Windows 上打包

```powershell
python -m pip install -r requirements.txt pyinstaller
pyinstaller --noconfirm --clean --onedir --windowed --name OfficeGitSync git_sync.py
```

构建目录为 `dist/OfficeGitSync/`，其中的 `OfficeGitSync.exe` 需要与同目录依赖文件一起分发。

## 在 Debian/Linux 上通过 Wine 打包 Windows 程序

仓库中的 `build_on_debian.sh` 是面向已有 Wine Windows-Python 环境的辅助脚本。运行前请确认 Wine 中已安装 Windows 版 Python，且命令 `wine python` 可以正常工作：

```bash
bash build_on_debian.sh
```

PyInstaller 不是通用的跨平台交叉编译器。此脚本依赖 Wine 中的 Windows Python 环境；在 ARM64 Android/NetHunter 环境中不保证可用。若脚本失败，建议回到之前已成功打包的 Debian 环境中执行。

## 自动构建

如果仓库的 GitHub Actions 工作流已启用，推送提交后可前往仓库的 **Actions** 页面查看运行状态和构建日志。只有工作流明确上传了 artifact 或发布了 Release 时，才会出现可下载的构建产物；推送成功本身不代表 Windows 程序已通过实机测试。

## 依赖

运行时依赖列在 [`requirements.txt`](requirements.txt) 中；打包时还需要 PyInstaller。

## English

OfficeGitSync is a small Windows-oriented file backup utility built with Watchdog and Dulwich. It watches filesystem events and uses a local Git repository to record file versions.

### Features

- Debounced filesystem monitoring.
- Uses Git status to limit prompts to changed paths involved in the current event, rather than listing every file in the watched directory.
- Filters common Office/WPS temporary files and supports custom exclusion patterns.
- Refuses to commit when unrelated files are already staged, reducing the risk of including unrelated content.
- Browse commit history and restore a selected version. **Restore resets the current branch and working tree** and may overwrite uncommitted changes or make newer commits disappear from the current branch view. Back up important data first.
- Windows startup entry and system-tray support.

### Safety notes

Do not rely on this application as your only backup. Git history is stored locally in the `.git` directory and is not automatically uploaded to GitHub. Direct deletion of individual commits is disabled in the current version because rewriting commit history can make other commits unreachable.

### Build on Windows

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt pyinstaller
pyinstaller --noconfirm --clean --onedir --windowed --name OfficeGitSync git_sync.py
```

The output is `dist/OfficeGitSync/`. Distribute the complete directory, not only the executable.

### Build from Debian/Linux with Wine

`build_on_debian.sh` expects Wine and a working Windows Python installation inside Wine. PyInstaller does not generally cross-compile between operating systems, and this workflow is not guaranteed to work on ARM64 Android/NetHunter.

### Automated builds

When the repository's GitHub Actions workflow is enabled, check the **Actions** tab for build status and logs. A downloadable artifact or Release is available only if the workflow is configured to publish one.
