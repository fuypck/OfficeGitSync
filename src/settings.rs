use crate::filters::FilterSettings;
use serde::{Deserialize, Serialize};
use std::{
    fs,
    io::{self, Write},
    path::{Path, PathBuf},
};

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Theme {
    #[default]
    System,
    Light,
    Dark,
}

#[derive(Clone, Serialize, Deserialize)]
#[serde(default)]
pub struct Settings {
    pub repo_path: String,
    pub filters: FilterSettings,
    pub auto_check: bool,
    pub interval_secs: u64,
    pub close_to_tray: bool,
    pub silent_start: bool,
    pub enable_log: bool,
    pub remote_name: String,
    pub remote_url: String,
    pub theme: Theme,
    pub last_export_parent: String,
}

impl Default for Settings {
    fn default() -> Self {
        Self {
            repo_path: std::env::current_dir()
                .unwrap_or_default()
                .display()
                .to_string(),
            filters: FilterSettings::default(),
            auto_check: false,
            interval_secs: 3,
            close_to_tray: true,
            silent_start: false,
            enable_log: false,
            remote_name: "origin".into(),
            remote_url: String::new(),
            theme: Theme::default(),
            last_export_parent: String::new(),
        }
    }
}

fn user_dir(var: &str) -> Option<PathBuf> {
    std::env::var_os(var)
        .map(PathBuf::from)
        .filter(|path| path.is_absolute())
}

pub fn config_dir() -> Result<PathBuf, String> {
    #[cfg(windows)]
    let base = user_dir("APPDATA").or_else(|| user_dir("USERPROFILE"));
    #[cfg(not(windows))]
    let base =
        user_dir("XDG_CONFIG_HOME").or_else(|| user_dir("HOME").map(|home| home.join(".config")));
    base.map(|base| base.join("OfficeGitSync"))
        .ok_or_else(|| "无法定位用户配置目录".into())
}

fn previous_config_path() -> Option<PathBuf> {
    user_dir("APPDATA")
        .or_else(|| user_dir("HOME"))
        .map(|base| base.join("OfficeGitSync").join("settings.json"))
}

fn read(path: &Path) -> Result<Option<Settings>, String> {
    match fs::read(path) {
        Ok(data) => serde_json::from_slice(&data)
            .map(Some)
            .map_err(|e| format!("配置 {} 无法解析：{e}", path.display())),
        Err(e) if e.kind() == io::ErrorKind::NotFound => Ok(None),
        Err(e) => Err(format!("无法读取配置 {}：{e}", path.display())),
    }
}

pub fn load() -> Result<Settings, String> {
    let file = config_dir()?.join("settings.json");
    if let Some(settings) = read(&file)? {
        return Ok(settings);
    }
    if let Some(old) = previous_config_path().filter(|old| *old != file) {
        if let Some(settings) = read(&old)? {
            return Ok(settings);
        }
    }
    Ok(Settings::default())
}

pub fn save(settings: &Settings) -> Result<(), String> {
    write(&config_dir()?.join("settings.json"), settings)
}

pub fn remember_export_parent(parent: &str) -> Result<(), String> {
    update_preferences(|settings| settings.last_export_parent = parent.to_owned())
}

pub fn remember_theme(theme: Theme) -> Result<(), String> {
    update_preferences(|settings| settings.theme = theme)
}

fn update_preferences(update: impl FnOnce(&mut Settings)) -> Result<(), String> {
    let mut settings = load()?;
    update(&mut settings);
    save(&settings)
}

fn write(path: &Path, settings: &Settings) -> Result<(), String> {
    settings.filters.compile()?;
    let parent = path.parent().ok_or("无法定位配置目录")?;
    fs::create_dir_all(parent).map_err(|e| e.to_string())?;
    // A malformed existing file may contain settings that can still be recovered.
    let _ = read(path)?;
    let mut temp = tempfile::NamedTempFile::new_in(parent).map_err(|e| e.to_string())?;
    serde_json::to_writer_pretty(&mut temp, settings).map_err(|e| e.to_string())?;
    temp.write_all(b"\n")
        .and_then(|()| temp.as_file().sync_all())
        .map_err(|e| e.to_string())?;
    temp.persist(path).map_err(|e| e.error.to_string())?;
    Ok(())
}

#[derive(Deserialize)]
#[serde(default)]
struct PythonSettings {
    watch_dir: String,
    debounce_seconds: u64,
    exclude_patterns: Vec<String>,
    enable_log: bool,
    silent_start: bool,
}

impl Default for PythonSettings {
    fn default() -> Self {
        Self {
            watch_dir: String::new(),
            debounce_seconds: 3,
            exclude_patterns: [
                "~$*",
                "*.tmp",
                "*.bak",
                "*.ffs_tmp",
                "*.ffs_lock",
                "*.ffs_db",
                "*.crdownload",
            ]
            .map(str::to_owned)
            .to_vec(),
            enable_log: false,
            silent_start: false,
        }
    }
}

pub fn import_python(path: &Path, current: &Settings) -> Result<Settings, String> {
    if path.as_os_str().is_empty() {
        return Err("请选择或输入 .officegitsync.json 文件路径".into());
    }
    let data = fs::read(path).map_err(|e| format!("无法读取 {}：{e}", path.display()))?;
    let value: serde_json::Value =
        serde_json::from_slice(&data).map_err(|e| format!("JSON 格式错误：{e}"))?;
    let object = value.as_object().ok_or("旧版配置应为 JSON 对象")?;
    // Distinguish the Python schema from Rust settings with similarly named preferences.
    if !["watch_dir", "exclude_patterns", "debounce_seconds"]
        .iter()
        .any(|key| object.contains_key(*key))
    {
        return Err("缺少旧版配置字段 watch_dir、exclude_patterns 或 debounce_seconds".into());
    }
    let old: PythonSettings =
        serde_json::from_slice(&data).map_err(|e| format!("旧版配置字段无效：{e}"))?;
    let mut result = current.clone();
    result.repo_path = old.watch_dir;
    result.interval_secs = old.debounce_seconds.clamp(1, 3600);
    result.enable_log = old.enable_log;
    result.silent_start = old.silent_start;
    result.auto_check = false;
    result.filters = FilterSettings::default();
    for preset in &mut result.filters.presets {
        preset.enabled = false;
    }
    result.filters.custom = old
        .exclude_patterns
        .into_iter()
        .filter(|rule| rule != ".git/*")
        .collect();
    result.filters.normalize();
    result.filters.compile()?;
    Ok(result)
}

pub fn append_log(event: &str) -> Result<(), String> {
    let dir = config_dir()?;
    fs::create_dir_all(&dir).map_err(|e| e.to_string())?;
    let mut file = fs::OpenOptions::new()
        .append(true)
        .create(true)
        .open(dir.join("officegitsync.log"))
        .map_err(|e| e.to_string())?;
    let event = event.replace(['\r', '\n'], " ");
    let now: chrono::DateTime<chrono::Utc> = std::time::SystemTime::now().into();
    writeln!(file, "{} {event}", now.to_rfc3339()).map_err(|e| e.to_string())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn persists_old_and_new_settings_and_preserves_invalid_config() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("settings.json");
        fs::write(&path, r#"{"repo_path":"旧目录","interval_secs":5}"#).unwrap();
        let mut settings = read(&path).unwrap().unwrap();
        assert_eq!(settings.repo_path, "旧目录");
        assert!(settings.close_to_tray);
        assert_eq!(settings.theme, Theme::System);
        assert!(settings.last_export_parent.is_empty());
        settings.silent_start = true;
        settings.theme = Theme::Dark;
        settings.last_export_parent = dir.path().display().to_string();
        settings.filters.custom.push("私有/**".into());
        write(&path, &settings).unwrap();
        let restored = read(&path).unwrap().unwrap();
        assert!(restored.silent_start);
        assert_eq!(restored.theme, Theme::Dark);
        assert_eq!(
            restored.last_export_parent,
            dir.path().display().to_string()
        );
        assert!(restored
            .filters
            .compile()
            .unwrap()
            .is_excluded("私有/报告.txt"));
        fs::write(&path, b"broken existing data").unwrap();
        assert!(write(&path, &settings).is_err());
        assert_eq!(fs::read(path).unwrap(), b"broken existing data");
    }

    #[test]
    fn imports_python_rules_and_keeps_rust_remote() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join(".officegitsync.json");
        fs::write(&path, r#"{"watch_dir":"资料","debounce_seconds":4,"exclude_patterns":["*.bak",".git/*"],"silent_start":true}"#).unwrap();
        let settings = Settings {
            remote_name: "backup".into(),
            ..Default::default()
        };
        let imported = import_python(&path, &settings).unwrap();
        assert_eq!(imported.repo_path, "资料");
        assert_eq!(imported.interval_secs, 4);
        assert_eq!(imported.remote_name, "backup");
        assert!(imported.silent_start);
        assert!(imported
            .filters
            .compile()
            .unwrap()
            .is_excluded("docs/old.bak"));
        assert!(!imported
            .filters
            .compile()
            .unwrap()
            .is_excluded("docs/current.tmp"));
    }

    #[test]
    fn python_import_reports_json_and_schema_errors() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join(".officegitsync.json");
        let current = Settings::default();
        assert!(import_python(&path, &current)
            .err()
            .unwrap()
            .contains("无法读取"));
        for (data, reason) in [
            ("{", "JSON 格式错误"),
            ("[]", "JSON 对象"),
            (
                r#"{"repo_path":"rust","enable_log":true}"#,
                "缺少旧版配置字段",
            ),
            (r#"{"watch_dir":5}"#, "配置字段无效"),
        ] {
            fs::write(&path, data).unwrap();
            assert!(import_python(&path, &current)
                .err()
                .unwrap()
                .contains(reason));
        }
    }
}
