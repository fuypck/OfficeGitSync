use globset::Glob;
use serde::{Deserialize, Serialize};

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Preset {
    pub name: String,
    pub enabled: bool,
    pub patterns: Vec<String>,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct FilterSettings {
    pub presets: Vec<Preset>,
    pub custom: Vec<String>,
}

impl Default for FilterSettings {
    fn default() -> Self {
        let preset = |name: &str, patterns: &[&str]| Preset {
            name: name.into(), enabled: true,
            patterns: patterns.iter().map(|s| (*s).into()).collect(),
        };
        Self {
            presets: vec![
                preset("Microsoft Office / WPS", &["~$*", "*.tmp"]),
                preset("LibreOffice", &[".~lock.*#"]),
                preset("Vim / Neovim", &["*.swp", "*.swo", "*.sw?", "*~", "*.un~"]),
                preset("Emacs", &["#*#", ".#*", "*~"]),
                preset("FreeFileSync", &["*.ffs_tmp", "*.ffs_lock", "*.ffs_db"]),
                preset("Browser downloads", &["*.crdownload", "*.part", "*.download"]),
            ],
            custom: Vec::new(),
        }
    }
}

impl FilterSettings {
    pub fn is_excluded(&self, path: &str) -> bool {
        let normalized = path.replace('\\', "/");
        if normalized.split('/').any(|part| part == ".git") { return true; }
        let filename = normalized.rsplit('/').next().unwrap_or(&normalized);
        self.presets.iter().filter(|p| p.enabled)
            .flat_map(|p| p.patterns.iter())
            .chain(self.custom.iter())
            .filter_map(|pattern| Glob::new(pattern).ok())
            .any(|glob| {
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
