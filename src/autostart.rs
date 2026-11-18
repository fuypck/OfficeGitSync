#[cfg(target_os = "linux")]
mod platform {
    use std::{
        fs,
        io::{self, Write},
        path::{Path, PathBuf},
    };

    const MARKER: &str = "X-OfficeGitSync-Rust=true";

    fn entry_path() -> io::Result<PathBuf> {
        let config = std::env::var_os("XDG_CONFIG_HOME")
            .map(PathBuf::from)
            .filter(|path| path.is_absolute())
            .or_else(|| std::env::var_os("HOME").map(|home| PathBuf::from(home).join(".config")))
            .ok_or_else(|| io::Error::other("无法定位用户配置目录"))?;
        Ok(config.join("autostart").join("officegitsync-rust.desktop"))
    }

    fn entry(path: &Path) -> io::Result<String> {
        let executable = path
            .to_str()
            .filter(|path| !path.contains(['\r', '\n']))
            .ok_or_else(|| io::Error::other("程序路径无法写入桌面启动项"))?;
        let mut quoted = String::new();
        for character in executable.chars() {
            match character {
                '\\' => quoted.push_str(r"\\\\"),
                '"' | '`' | '$' => {
                    quoted.push_str(r"\\");
                    quoted.push(character);
                }
                '%' => quoted.push_str("%%"),
                '\t' => quoted.push_str(r"\t"),
                character => quoted.push(character),
            }
        }
        Ok(format!(
            "[Desktop Entry]\nType=Application\nName=OfficeGitSync\nExec=\"{quoted}\"\n{MARKER}\n"
        ))
    }

    fn managed(path: &Path) -> io::Result<bool> {
        match fs::symlink_metadata(path) {
            Ok(metadata) if metadata.is_file() => {
                if fs::read_to_string(path)?.lines().any(|line| line == MARKER) {
                    Ok(true)
                } else {
                    Err(io::Error::other("启动项已存在，且不属于当前程序"))
                }
            }
            Ok(_) => Err(io::Error::other("启动项路径已被其他文件占用")),
            Err(e) if e.kind() == io::ErrorKind::NotFound => Ok(false),
            Err(e) => Err(e),
        }
    }

    pub fn enabled() -> io::Result<bool> {
        managed(&entry_path()?)
    }

    pub fn set(enabled: bool) -> io::Result<()> {
        let path = entry_path()?;
        let present = managed(&path)?;
        if enabled {
            let parent = path.parent().expect("autostart parent");
            fs::create_dir_all(parent)?;
            let mut temp = tempfile::NamedTempFile::new_in(parent)?;
            temp.write_all(entry(&std::env::current_exe()?)?.as_bytes())?;
            if present {
                temp.persist(path)?;
            } else {
                temp.persist_noclobber(path)?;
            }
        } else if present {
            fs::remove_file(path)?;
        }
        Ok(())
    }

    #[cfg(test)]
    mod tests {
        use super::*;

        #[test]
        fn desktop_entry_escapes_path_and_preserves_unowned_entries() {
            let entry = entry(Path::new(r#"/tmp/有 空格/100%$"\x"#)).unwrap();
            assert!(
                entry.contains(r#"Exec="/tmp/有 空格/100%%\\$\\"\\\\x""#),
                "{entry}"
            );
            let dir = tempfile::tempdir().unwrap();
            let path = dir.path().join("app.desktop");
            assert!(!managed(&path).unwrap());
            fs::write(&path, "other").unwrap();
            assert!(managed(&path).is_err());
            fs::write(&path, entry).unwrap();
            assert!(managed(&path).unwrap());
        }
    }
}

#[cfg(target_os = "windows")]
mod platform {
    use std::{ffi::OsString, io};
    use winreg::{enums::*, RegKey};

    const KEY: &str = r"Software\Microsoft\Windows\CurrentVersion\Run";
    const VALUE: &str = "OfficeGitSyncRust";

    pub fn enabled() -> io::Result<bool> {
        match RegKey::predef(HKEY_CURRENT_USER)
            .open_subkey(KEY)
            .and_then(|key| key.get_value::<OsString, _>(VALUE))
        {
            Ok(_) => Ok(true),
            Err(e) if e.kind() == io::ErrorKind::NotFound => Ok(false),
            Err(e) => Err(e),
        }
    }

    pub fn set(enabled: bool) -> io::Result<()> {
        let user = RegKey::predef(HKEY_CURRENT_USER);
        if enabled {
            let executable = std::env::current_exe()?;
            let mut command = OsString::from("\"");
            command.push(executable.as_os_str());
            command.push("\"");
            let (key, _) = user.create_subkey(KEY)?;
            key.set_value(VALUE, &command)?;
        } else if self::enabled()? {
            user.open_subkey_with_flags(KEY, KEY_SET_VALUE)?
                .delete_value(VALUE)?;
        }
        Ok(())
    }
}

#[cfg(any(target_os = "linux", target_os = "windows"))]
pub use platform::{enabled, set};

#[cfg(not(any(target_os = "linux", target_os = "windows")))]
pub fn enabled() -> std::io::Result<bool> {
    Ok(false)
}

#[cfg(not(any(target_os = "linux", target_os = "windows")))]
pub fn set(_: bool) -> std::io::Result<()> {
    Err(std::io::Error::new(
        std::io::ErrorKind::Unsupported,
        "当前平台不支持登录启动",
    ))
}
