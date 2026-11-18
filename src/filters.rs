use globset::Glob;
use serde::{Deserialize, Serialize};
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Preset { pub name: String, pub enabled: bool, pub patterns: Vec<String> }
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct FilterSettings { pub presets: Vec<Preset>, pub custom: Vec<String> }
impl Default for FilterSettings {
    fn default() -> Self {
        let preset = |name: &str, patterns: &[&str]| Preset {
            name: name.into(), enabled: true,
            patterns: patterns.iter().map(|s| (*s).into()).collect(),
        };
        Self { presets: vec![
            preset("Office / WPS", &["~$*", "*.tmp"]),
            preset("LibreOffice", &[".~lock.*#"]),
            preset("Vim / Neovim", &["*.swp", "*.swo", "*.sw?", "*~", "*.un~"]),
            preset("Emacs", &["#*#", ".#*", "*~"]),
            preset("FreeFileSync", &["*.ffs_tmp", "*.ffs_lock", "*.ffs_db"]),
            preset("浏览器下载", &["*.crdownload", "*.part", "*.download"]),
        ], custom: Vec::new() }
    }
}
impl FilterSettings {
    pub fn active_rules(&self) -> Vec<(String, String)> {
        let mut out = Vec::new();
        for p in self.presets.iter().filter(|p| p.enabled) {
            for rule in &p.patterns {
                if !rule.trim().is_empty() { out.push((p.name.clone(), rule.clone())); }
            }
        }
        for rule in &self.custom {
            if !rule.trim().is_empty() { out.push(("自定义".into(), rule.clone())); }
        }
        out
    }
    pub fn is_excluded(&self, path: &str) -> bool {
        let normalized = path.replace('\\', "/");
        if normalized.split('/').any(|part| part == ".git") { return true; }
        let filename = normalized.rsplit('/').next().unwrap_or(&normalized);
        self.active_rules().iter().filter_map(|(_, p)| Glob::new(p).ok()).any(|glob| {
            let matcher = glob.compile_matcher();
            matcher.is_match(filename) || matcher.is_match(&normalized)
        })
    }
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn filters_common_temp_files() {
        let f = FilterSettings::default();
        assert!(f.is_excluded("docs/.~lock.report.docx#"));
        assert!(f.is_excluded("notes.swp"));
        assert!(f.is_excluded("folder/.git/config"));
        assert!(!f.is_excluded("docs/report.docx"));
    }
}
