#!/usr/bin/env bash
set -Eeuo pipefail

cd "$(dirname "$0")"

if ! command -v wine >/dev/null 2>&1; then
    echo "错误：未找到 Wine。请在 Debian/Linux 上安装并配置 Wine。" >&2
    exit 1
fi

if ! wine python --version; then
    echo "错误：Wine 中未找到可用的 Windows Python。请先安装 Windows 版 Python，并确认 'wine python' 可运行。" >&2
    exit 1
fi

echo "正在安装 Windows Python 依赖..."
wine python -m pip install --upgrade pip
wine python -m pip install dulwich watchdog pystray Pillow pyinstaller

echo "开始打包 Windows 程序..."
wine pyinstaller --noconfirm --clean --onedir --windowed --name "OfficeGitSync" git_sync.py

if [[ ! -f "dist/OfficeGitSync/OfficeGitSync.exe" ]]; then
    echo "错误：未找到预期产物 dist/OfficeGitSync/OfficeGitSync.exe。请检查上面的打包日志。" >&2
    exit 1
fi

echo "打包完成：dist/OfficeGitSync/OfficeGitSync.exe"
echo "注意：请在 Windows 上实际启动并测试后再发布。"
