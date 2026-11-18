use std::{io::ErrorKind, os::unix::ffi::OsStrExt, path::PathBuf, process::Command};

pub fn pick_python_config() -> Result<Option<PathBuf>, String> {
    let zenity = [
        "--file-selection",
        "--title=选择旧版 Python 配置",
        "--file-filter=旧版 Python 配置 | .officegitsync.json *.json",
    ];
    let kdialog = [
        "--title",
        "选择旧版 Python 配置",
        "--getopenfilename",
        "",
        "*.json|旧版 Python 配置",
    ];
    pick(&zenity, &kdialog)
}

pub fn pick_remote_folder() -> Result<Option<PathBuf>, String> {
    pick(
        &[
            "--file-selection",
            "--directory",
            "--title=选择远程仓库目录",
        ],
        &["--title", "选择远程仓库目录", "--getexistingdirectory"],
    )
}

fn pick(zenity: &[&str], kdialog: &[&str]) -> Result<Option<PathBuf>, String> {
    for (program, args) in [("zenity", zenity), ("kdialog", kdialog)] {
        let output = match Command::new(program).args(args).output() {
            Ok(output) => output,
            Err(e) if e.kind() == ErrorKind::NotFound => continue,
            Err(e) => return Err(format!("无法启动 {program}：{e}")),
        };
        if output.status.success() {
            let path = output.stdout.strip_suffix(b"\n").unwrap_or(&output.stdout);
            return Ok((!path.is_empty()).then(|| PathBuf::from(std::ffi::OsStr::from_bytes(path))));
        }
        let error = String::from_utf8_lossy(&output.stderr);
        if output.status.code() == Some(1) && error.trim().is_empty() {
            return Ok(None);
        }
        return Err(format!(
            "{program} 返回 {}：{}",
            output.status,
            error.trim()
        ));
    }
    Err("未找到桌面文件选择器 Zenity 或 KDialog，请安装其中之一或手动输入路径".into())
}
