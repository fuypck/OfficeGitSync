# OfficeGitSync

OfficeGitSync Rust **0.2.1 Rust 预览版**是一个面向工作文档的本地版本备份工具。它监听指定目录、提示用户确认备份，并把选中的文件变更记录到 Git。应用使用简体中文桌面界面，支持 Windows 和 Linux。

[下载 Windows x64 预览版](https://github.com/fuypck/OfficeGitSync/releases/tag/v0.2.1-rust-preview) · [English summary](#english-summary) · [产品需求与设计原则](prompt.txt) · [Rust 迁移记录](RUST_REWRITE.md)

## 功能

- **文档备份**将递归文件事件合并后列出相关 Git 变更，也可手动扫描。勾选文件后确认提交，说明预填当前本地时间，可直接编辑。
- **历史版本**显示当前分支及旧版时间线的提交、说明、时间和标签。选中版本后可导出到独立子目录，或创建新标签。
- **设置**管理主题、监听防抖、排除规则、可选日志、系统托盘、静默启动与登录启动。支持通过「浏览…」或手动输入路径导入旧版 `.officegitsync.json`。Linux 文件选择器使用 Zenity 或 KDialog，隐藏配置可用 Ctrl+H 显示。
- **Git 高级**提供文本差异、备份标签及远程仓库配置。支持 Fetch 和手动推送当前分支到本地裸仓库。

本地初始化、扫描、提交、历史浏览和导出由内置 libgit2 执行，系统无需安装 `git` 命令。Git 仓库保存在所选工作目录中。配置、实例锁和启用后的日志保存在用户配置目录。

## 平台支持

| 项目 | Windows | Linux |
| --- | --- | --- |
| 工作和导出目录 | 手动输入或原生文件夹选择器 | 手动输入 |
| 简体中文字体 | 优先使用微软雅黑常规体，并校验中文覆盖 | 通过 Fontconfig 查找简体中文字体 |
| 后台与托盘 | Windows 系统托盘 | 需要支持 StatusNotifierItem 的桌面会话 |
| 当前用户登录启动 | 注册表 Run 项 | XDG Autostart |
| 配置目录 | `%APPDATA%\OfficeGitSync` | `$XDG_CONFIG_HOME/OfficeGitSync`，默认 `~/.config/OfficeGitSync` |

Debian/Kali 可用 `sudo apt install fontconfig fonts-noto-cjk` 提供中文字体。托盘不可用时，关闭窗口会退出。静默启动需要已保存的有效工作目录和启用的监听设置。

## 使用

1. 启动 `OfficeGitSync.exe`（Windows）或 `OfficeGitSync`（Linux）。在「文档备份」输入一个已有目录。已有 Git 仓库可直接扫描，新目录可点击「初始化 Git 仓库」。
2. 启用「监听工作目录」或点击「扫描变更」。勾选要备份的文件，编辑提交说明，然后点击「确认备份」。
3. 如需查看旧内容，打开「历史版本」，选中提交，输入工作区外已存在的目标目录，再确认导出。应用会在目标目录下创建新子目录，并记住上次成功使用的目标目录。
4. 在「设置」保存监听、工作目录等偏好，也可编辑内置和自定义排除规则。主题选项切换后立即应用并保存。可选日志用于记录备份和监听事件，默认关闭。

再次启动同一用户的应用时，会唤醒正在运行的窗口。

## Git 安全机制

- 监听只提示待确认的变更。提交前会核对扫描时的分支和提交、选中文件、暂存区和索引锁。遇到已有暂存内容、合并、变基或分离 HEAD 等情况，备份会停止并提示。
- 应用先在独立索引中按字面路径暂存，再核对将提交的变更。需要 Git LFS 等专用内容转换的文件会提示使用支持该转换的客户端。
- 版本导出保留原工作区、Git 索引和分支指针。导出拒绝符号链接、子模块和不安全的路径。标签保留已有同名引用。
- 备份建立在本地 Git 提交上。Fetch 和向本地裸仓库推送是显式操作。GitHub 等网络 URL 保留 Fetch，Pull、网络 URL 推送和自动云同步尚未实现。

## 本地和局域网远程仓库

在「Git 高级 → 远程仓库」输入 Git URL、SSH 地址或目录，也可点击「浏览…」选择本地或已挂载的网络文件夹。Windows 支持完整 UNC 共享路径，Linux 上请先挂载共享目录。相对路径以当前工作区为基准。

点击「检查并保存 Remote」后，已有仓库可直接登记，工作区外的空目录需要明确确认才会初始化为裸仓库。带工作区的仓库可 Fetch，本地或挂载共享上的裸仓库也可手动推送当前分支。应用保留普通非空目录和已有仓库内容，并拒绝自动覆盖非快进的远程分支。

## Rust 构建

使用 Rust stable 工具链。构建前确认工作树位于 `rust-rewrite` 分支。

```sh
cargo fmt --check
cargo check --locked
cargo test --locked
cargo clippy --locked --all-targets -- -D warnings
cargo build --locked --release
```

本机构建产物位于 Windows 的 `target/release/OfficeGitSync.exe` 或 Linux 的 `target/release/OfficeGitSync`。Windows 本机构建使用支持 Rust MSVC 工具链的 Visual Studio C++ 生成工具及 Windows SDK。Linux 需要 C 编译工具链、`pkg-config` 和 OpenSSL 开发文件。依赖已缓存时可为 Cargo 构建、检查、测试和 Clippy 命令加 `--offline`。

在 Linux 上使用 MinGW-w64 交叉构建 Windows x64：

```sh
rustup target add x86_64-pc-windows-gnu
CARGO_TARGET_X86_64_PC_WINDOWS_GNU_LINKER=x86_64-w64-mingw32-gcc \
  cargo build --locked --release --target x86_64-pc-windows-gnu
```

交叉构建产物为 `target/x86_64-pc-windows-gnu/release/OfficeGitSync.exe`。常规构建使用 [Cargo.toml](Cargo.toml) 中的版本 `0.2.1`。此预览版发布标签为 `v0.2.1-rust-preview`，附件由此发布标签所指提交在 Windows CI 中构建。发布工作流构建 `v*` 标签时会把标签版本传给程序。预览构建可在 [GitHub Actions](https://github.com/fuypck/OfficeGitSync/actions) 中查看，已发布版本以 [GitHub Releases](https://github.com/fuypck/OfficeGitSync/releases) 为准。

## 项目状态

`rust-rewrite` 是 Rust 预览分支。仍待完善的功能包括 Linux 原生目录选择、复杂远程认证、Office 二进制文档的语义差异和 Linux 安装包。历史删除及原地恢复暂缓。Windows 文件监听的递归测试在 Wine 中会超时，Windows 实机结果需单独验证。

Rust 源码位于 `src/`，程序图标及其源文件位于 `assets/`，依赖锁定文件为 `Cargo.lock`。旧版源码和打包脚本作为迁移参考保留，Rust 构建不依赖它们。

作者 Ed Clack · [GitHub 项目](https://github.com/fuypck/OfficeGitSync) · [蓝奏云](https://wwbvu.lanzoum.com/b00egzj8wb)（访问密码 `1jjn`）

## English summary

OfficeGitSync is a Simplified Chinese desktop app for local document version backups on Windows and Linux. The Rust preview uses libgit2 for local repository initialization, selected-file commits, history browsing, and version export.

Choose a working directory, enable monitoring or scan manually, and confirm the files you want to commit. To recover a version, export it into a new subdirectory outside the working tree. Windows offers native folder pickers. On Linux, Chinese text uses a Fontconfig-discovered font, and the tray requires a StatusNotifierItem host.

[Download the Windows x64 Rust preview](https://github.com/fuypck/OfficeGitSync/releases/tag/v0.2.1-rust-preview). Build with Rust stable and `cargo build --locked --release`. See [Rust build instructions](#rust-构建) for the Windows x64 cross-build. The current branch supports text diffs, explicit Fetch, and non-forced pushes to local bare repositories, including mounted shares. URL pushes, Pull, in-place restoration, and history deletion are outside the current implementation.
