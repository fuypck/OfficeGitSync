#!/bin/bash
echo "正在准备 Windows 打包环境..."

# 下载 Windows 版 Python 便携包/安装包，或直接用 Wine 运行 PyInstaller
wine python -m pip install psutil pystray Pillow pyinstaller

echo "开始编译 Windows 可执行文件..."
wine pyinstaller --noconfirm --onedir --windowed --name "OfficeGitSync" git_sync.py

echo "打包完成！产物位于 dist/OfficeGitSync/ 目录中"
