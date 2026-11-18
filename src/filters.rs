use globset::{GlobBuilder, GlobSet, GlobSetBuilder};
use serde::{Deserialize, Serialize};
use std::collections::HashSet;
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Preset {
    pub name: String,
    pub enabled: bool,
    pub patterns: Vec<String>,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(default)]
pub struct FilterSettings {
    pub presets: Vec<Preset>,
    pub custom: Vec<String>,
}
impl Default for FilterSettings {
    fn default() -> Self {
        let preset = |name: &str, patterns: &[&str]| Preset {
            name: name.into(),
            enabled: true,
            patterns: patterns.iter().map(|s| (*s).into()).collect(),
        };
        Self {
            presets: vec![
                preset("Office / WPS", &["~$*", "*.tmp", "*.bak"]),
                preset("LibreOffice", &[".~lock.*#"]),
                preset("Vim / Neovim", &["*.swp", "*.swo", "*.sw?", "*~", "*.un~"]),
                preset("Emacs", &["#*#", ".#*", "*~"]),
                preset("FreeFileSync", &["*.ffs_tmp", "*.ffs_lock", "*.ffs_db"]),
                preset("浏览器下载", &["*.crdownload", "*.part", "*.download"]),
            ],
            custom: Vec::new(),
        }
    }
}
impl FilterSettings {
    pub fn active_rules(&self) -> Vec<(String, String)> {
        let mut out = Vec::new();
        for p in self.presets.iter().filter(|p| p.enabled) {
            for rule in &p.patterns {
                if !rule.trim().is_empty() {
                    out.push((p.name.clone(), rule.clone()));
                }
            }
        }
        for rule in &self.custom {
            if !rule.trim().is_empty() {
                out.push(("自定义".into(), rule.clone()));
            }
        }
        out
    }
    pub fn normalize(&mut self) {
        for patterns in self
            .presets
            .iter_mut()
            .map(|preset| &mut preset.patterns)
            .chain(std::iter::once(&mut self.custom))
        {
            let mut seen = HashSet::new();
            for pattern in patterns.iter_mut() {
                *pattern = pattern.trim().to_owned();
            }
            patterns.retain(|pattern| !pattern.is_empty() && seen.insert(pattern.clone()));
        }
    }

    pub fn compile(&self) -> Result<FilterMatcher, String> {
        let mut builder = GlobSetBuilder::new();
        for (source, rule) in self.active_rules() {
            let glob = GlobBuilder::new(&rule)
                .case_insensitive(cfg!(windows))
                .build()
                .map_err(|e| format!("「{source}」中的规则「{rule}」无效：{e}"))?;
            builder.add(glob);
        }
        builder
            .build()
            .map(FilterMatcher)
            .map_err(|e| e.to_string())
    }
}

pub struct FilterMatcher(GlobSet);

impl FilterMatcher {
    pub fn is_excluded(&self, path: &str) -> bool {
        #[cfg(windows)]
        let normalized = path.replace('\\', "/");
        #[cfg(windows)]
        let path = normalized.as_str();
        if path
            .split('/')
            .any(|part| part.eq_ignore_ascii_case(".git"))
        {
            return true;
        }
        let filename = path.rsplit('/').next().unwrap_or(path);
        self.0.is_match(filename) || self.0.is_match(path)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn filters_common_temp_files() {
        let f = FilterSettings::default().compile().unwrap();
        assert!(f.is_excluded("docs/.~lock.report.docx#"));
        assert!(f.is_excluded("notes.swp"));
        assert!(f.is_excluded("docs/archive.bak"));
        assert!(f.is_excluded("folder/.git/config"));
        assert!(!f.is_excluded("docs/report.docx"));
    }

    #[test]
    fn custom_rules_normalize_and_invalid_globs_are_reported() {
        let mut settings = FilterSettings {
            custom: vec![" 私有/** ".into(), "私有/**".into(), String::new()],
            ..Default::default()
        };
        settings.normalize();
        assert_eq!(settings.custom, ["私有/**"]);
        assert!(settings
            .compile()
            .unwrap()
            .is_excluded("私有/资料/报告.docx"));
        assert!(!settings
            .compile()
            .unwrap()
            .is_excluded("公开/资料/报告.docx"));
        settings.custom.push("[".into());
        assert!(settings.compile().is_err());
    }
}
