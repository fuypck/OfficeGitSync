#![cfg_attr(target_os = "windows", windows_subsystem = "windows")]
mod filters;
use eframe::egui;
use git2::{Repository, StatusOptions, DiffOptions, Signature};
use serde::{Deserialize, Serialize};
use std::{path::PathBuf, time::{Duration, Instant}, fs};

#[derive(Clone, Serialize, Deserialize)]
#[serde(default)]
struct Settings {
    repo_path: String,
    filters: filters::FilterSettings,
    auto_check: bool,
    interval_secs: u64,
    remote_name: String,
    remote_url: String,
}
impl Default for Settings {
    fn default() -> Self {
        Self {
            repo_path: std::env::current_dir().unwrap_or_else(|_| PathBuf::from(".")).display().to_string(),
            filters: filters::FilterSettings::default(),
            auto_check: false,
            interval_secs: 5,
            remote_name: "origin".into(),
            remote_url: String::new(),
        }
    }
}
fn config_path() -> PathBuf {
    let base = std::env::var_os("APPDATA").map(PathBuf::from)
        .or_else(|| std::env::var_os("HOME").map(PathBuf::from))
        .unwrap_or_else(|| PathBuf::from("."));
    base.join("OfficeGitSync").join("settings.json")
}
fn save_settings(settings: &Settings) -> Result<(), String> {
    let path = config_path();
    if let Some(parent) = path.parent() { fs::create_dir_all(parent).map_err(|e| e.to_string())?; }
    let tmp = path.with_extension("tmp");
    fs::write(&tmp, serde_json::to_vec_pretty(settings).map_err(|e| e.to_string())?).map_err(|e| e.to_string())?;
    fs::rename(tmp, path).map_err(|e| e.to_string())
}
fn load_settings() -> Settings {
    fs::read(config_path()).ok().and_then(|b| serde_json::from_slice(&b).ok()).unwrap_or_default()
}
#[derive(Clone)]
struct Change { path: String, status: String, selected: bool }
struct App {
    settings: Settings,
    changes: Vec<Change>,
    history: Vec<(String, String)>,
    message: String,
    diff: String,
    selected_path: String,
    notice: String,
    advanced: bool,
    tab: usize,
    custom_editor: String,
    last_check: Instant,
    pending_backup: bool,
    previous_snapshot: Vec<String>,
}
impl Default for App {
    fn default() -> Self {
        let settings = load_settings();
        let custom_editor = settings.filters.custom.join("\n");
        let mut app = Self { settings, changes: Vec::new(), history: Vec::new(),
            message: "文档备份".into(), diff: String::new(), selected_path: String::new(),
            notice: String::new(), advanced: false, tab: 0, custom_editor,
            last_check: Instant::now(), pending_backup: false, previous_snapshot: Vec::new() };
        app.refresh();
        app
    }
}
impl App {
    fn repo(&self) -> Result<Repository, String> { Repository::open(&self.settings.repo_path).map_err(|e| e.to_string()) }
    fn snapshot(&self, items: &[Change]) -> Vec<String> {
        items.iter().map(|c| {
            let full = PathBuf::from(&self.settings.repo_path).join(&c.path);
            let stamp = fs::metadata(full).ok().map(|m| {
                let time = m.modified().ok().and_then(|t| t.duration_since(std::time::UNIX_EPOCH).ok());
                format!("{}:{:?}", m.len(), time)
            }).unwrap_or_else(|| "deleted".into());
            format!("{}:{}:{}", c.path, c.status, stamp)
        }).collect()
    }
    fn refresh(&mut self) {
        match self.read_status() {
            Ok(changes) => {
                self.changes = changes;
                self.previous_snapshot = self.snapshot(&self.changes);
                self.notice = format!("已刷新，共 {} 项变更", self.changes.len());
            }
            Err(e) => self.notice = format!("仓库读取失败：{e}"),
        }
        self.read_history();
    }
    fn read_status(&self) -> Result<Vec<Change>, String> {
        let repo = self.repo()?;
        let mut opts = StatusOptions::new();
        opts.include_untracked(true).recurse_untracked_dirs(true).include_ignored(false);
        let statuses = repo.statuses(Some(&mut opts)).map_err(|e| e.to_string())?;
        let mut items = Vec::new();
        for entry in statuses.iter() {
            let path = entry.path().unwrap_or("").to_string();
            if path.is_empty() || self.settings.filters.is_excluded(&path) { continue; }
            items.push(Change { path, status: format!("{:?}", entry.status()), selected: true });
        }
        Ok(items)
    }
    fn read_history(&mut self) {
        self.history.clear();
        if let Ok(repo) = self.repo() {
            if let Ok(mut walk) = repo.revwalk() {
                if walk.push_head().is_ok() {
                    for oid in walk.take(100).flatten() {
                        if let Ok(commit) = repo.find_commit(oid) {
                            self.history.push((oid.to_string(), commit.summary().unwrap_or("(无说明)").to_string()));
                        }
                    }
                }
            }
        }
    }
    fn init_repo(&mut self) {
        match Repository::init(&self.settings.repo_path) {
            Ok(_) => self.refresh(),
            Err(e) => self.notice = format!("初始化失败：{e}"),
        }
    }
    fn commit_selected(&mut self) {
        let result = (|| -> Result<String, String> {
            let repo = self.repo()?;
            let selected: Vec<String> = self.changes.iter().filter(|c| c.selected).map(|c| c.path.clone()).collect();
            if selected.is_empty() { return Err("没有选择要提交的文件".into()); }
            let mut index = repo.index().map_err(|e| e.to_string())?;
            if index.iter().any(|entry| {
                let p = String::from_utf8_lossy(&entry.path);
                !selected.iter().any(|s| s == p.as_ref())
            }) { return Err("暂存区含有其他文件；为避免误提交，请先在其他 Git 客户端处理暂存区".into()); }
            for path in &selected {
                let full = repo.workdir().ok_or("裸仓库无法提交工作区")?.join(path);
                if full.exists() { index.add_path(std::path::Path::new(path)).map_err(|e| e.to_string())?; }
                else { index.remove_path(std::path::Path::new(path)).map_err(|e| e.to_string())?; }
            }
            index.write().map_err(|e| e.to_string())?;
            let tree_id = index.write_tree().map_err(|e| e.to_string())?;
            let tree = repo.find_tree(tree_id).map_err(|e| e.to_string())?;
            let signature = repo.signature().or_else(|_| Signature::now("OfficeGitSync", "officegitsync@localhost")).map_err(|e| e.to_string())?;
            let parent = repo.head().ok().and_then(|h| h.peel_to_commit().ok());
            let parents: Vec<&git2::Commit<'_>> = parent.iter().collect();
            let oid = repo.commit(Some("HEAD"), &signature, &signature,
                if self.message.trim().is_empty() { "文档备份" } else { self.message.trim() },
                &tree, &parents).map_err(|e| e.to_string())?;
            Ok(oid.to_string())
        })();
        match result {
            Ok(id) => { self.pending_backup = false; self.refresh(); self.notice = format!("提交成功：{}", &id[..8]); }
            Err(e) => self.notice = format!("提交失败：{e}"),
        }
    }
    fn show_diff(&mut self, path: &str) {
        let result = (|| -> Result<String, String> {
            let repo = self.repo()?;
            let mut opts = DiffOptions::new();
            opts.pathspec(path);
            let diff = repo.diff_index_to_workdir(None, Some(&mut opts)).map_err(|e| e.to_string())?;
            let mut out = String::new();
            diff.print(git2::DiffFormat::Patch, |_delta, _hunk, line| {
                out.push(line.origin());
                out.push_str(&String::from_utf8_lossy(line.content()));
                true
            }).map_err(|e| e.to_string())?;
            if out.is_empty() { out = "无文本差异（Office 二进制文件暂不支持语义 Diff，或文件尚未加入索引）".into(); }
            Ok(out)
        })();
        self.diff = result.unwrap_or_else(|e| format!("Diff 失败：{e}"));
        self.selected_path = path.into();
    }
    fn remote_action(&mut self, action: &str) {
        let result = (|| -> Result<String, String> {
            let repo = self.repo()?;
            match action {
                "add" => { repo.remote(&self.settings.remote_name, &self.settings.remote_url).map_err(|e| e.to_string())?; Ok("远程仓库已添加".into()) }
                "fetch" => {
                    let mut remote = repo.find_remote(&self.settings.remote_name).map_err(|e| e.to_string())?;
                    remote.fetch(&[] as &[&str], None, None).map_err(|e| e.to_string())?;
                    Ok("Fetch 完成".into())
                }
                _ => Err("不支持的操作".into())
            }
        })();
        self.notice = result.unwrap_or_else(|e| format!("远程操作失败：{e}"));
    }
    fn save(&mut self) {
        self.settings.filters.custom = self.custom_editor.lines().map(str::trim)
            .filter(|s| !s.is_empty()).map(str::to_owned).collect();
        self.notice = match save_settings(&self.settings) { Ok(()) => "设置已保存".into(), Err(e) => format!("保存失败：{e}") };
    }
}
impl eframe::App for App {
    fn update(&mut self, ctx: &egui::Context, _: &mut eframe::Frame) {
        if self.settings.auto_check && self.last_check.elapsed() >= Duration::from_secs(self.settings.interval_secs.max(2)) {
            self.last_check = Instant::now();
            if let Ok(items) = self.read_status() {
                let snapshot = self.snapshot(&items);
                if snapshot != self.previous_snapshot {
                    self.changes = items;
                    self.previous_snapshot = snapshot;
                    self.pending_backup = !self.changes.is_empty();
                    if self.pending_backup { self.notice = "检测到文件变化，请确认是否备份".into(); }
                }
            }
        }
        if self.settings.auto_check { ctx.request_repaint_after(Duration::from_secs(2)); }
        egui::CentralPanel::default().show(ctx, |ui| {
            ui.heading(format!("OfficeGitSync {}  ·  Rust 测试版", env!("OFFICEGITSYNC_VERSION")));
            ui.horizontal(|ui| {
                ui.selectable_value(&mut self.advanced, false, "文档备份");
                ui.selectable_value(&mut self.advanced, true, "Git 高级模式");
            });
            ui.horizontal(|ui| {
                ui.label("工作目录：");
                ui.add_sized([350.0, 22.0], egui::TextEdit::singleline(&mut self.settings.repo_path));
                if ui.button("刷新").clicked() { self.refresh(); }
                if ui.button("初始化 Git 仓库").clicked() { self.init_repo(); }
            });
            if !self.notice.is_empty() { ui.label(&self.notice); }
            ui.separator();
            if !self.advanced {
                ui.horizontal(|ui| {
                    ui.checkbox(&mut self.settings.auto_check, "自动检测变更（提交前确认）");
                    ui.label("检查间隔（秒）");
                    ui.add(egui::DragValue::new(&mut self.settings.interval_secs).range(2..=3600));
                    if ui.button("保存设置").clicked() { self.save(); }
                });
                if self.pending_backup { ui.colored_label(egui::Color32::YELLOW, "检测到变更，等待人工确认备份"); }
            } else {
                ui.horizontal(|ui| {
                    for (id, name) in ["工作区", "提交历史", "远程仓库", "排除规则"].iter().enumerate() {
                        ui.selectable_value(&mut self.tab, id, *name);
                    }
                });
            }
            if !self.advanced || self.tab == 0 {
                ui.heading("当前文件变更");
                egui::ScrollArea::vertical().max_height(210.0).show(ui, |ui| {
                    let mut selected = None;
                    for item in &mut self.changes {
                        ui.horizontal(|ui| {
                            ui.checkbox(&mut item.selected, "");
                            if ui.button(&item.path).clicked() { selected = Some(item.path.clone()); }
                            ui.small(&item.status);
                        });
                    }
                    if let Some(path) = selected { self.show_diff(&path); }
                });
                ui.horizontal(|ui| {
                    ui.label("提交说明：");
                    ui.text_edit_singleline(&mut self.message);
                    if ui.button("确认提交选中文件").clicked() { self.commit_selected(); }
                });
                if !self.selected_path.is_empty() {
                    ui.collapsing(format!("差异查看：{}", self.selected_path), |ui| {
                        egui::ScrollArea::vertical().max_height(180.0).show(ui, |ui| {
                            ui.add(egui::TextEdit::multiline(&mut self.diff).font(egui::TextStyle::Monospace).desired_width(f32::INFINITY));
                        });
                    });
                }
            }
            if self.advanced && self.tab == 1 {
                ui.heading("提交历史（只读）");
                egui::ScrollArea::vertical().show(ui, |ui| {
                    for (id, summary) in &self.history { ui.label(format!("{}  {}", &id[..8], summary)); }
                });
            }
            if self.advanced && self.tab == 2 {
                ui.heading("远程仓库");
                ui.label("Remote 名称");
                ui.text_edit_singleline(&mut self.settings.remote_name);
                ui.label("Git URL / 本地目录 / 局域网共享路径");
                ui.text_edit_singleline(&mut self.settings.remote_url);
                ui.horizontal(|ui| {
                    if ui.button("添加 Remote").clicked() { self.remote_action("add"); }
                    if ui.button("Fetch").clicked() { self.remote_action("fetch"); }
                    if ui.button("保存配置").clicked() { self.save(); }
                });
                ui.label("HTTPS / SSH 认证使用系统 Git 环境；复杂认证与 Push/Pull 尚待完善。");
            }
            if !self.advanced || self.tab == 3 {
                ui.separator();
                ui.collapsing("排除规则管理", |ui| {
                    for preset in &mut self.settings.filters.presets {
                        ui.horizontal(|ui| {
                            ui.checkbox(&mut preset.enabled, &preset.name);
                            ui.label(format!("{} 条规则", preset.patterns.len()));
                        });
                        if preset.enabled {
                            ui.indent(preset.name.as_str(), |ui| {
                                for pattern in &mut preset.patterns { ui.text_edit_singleline(pattern); }
                            });
                        }
                    }
                    ui.separator();
                    ui.label("自定义规则（每行一条，可直接编辑）");
                    ui.add(egui::TextEdit::multiline(&mut self.custom_editor).desired_rows(3));
                    ui.label("当前生效的全部规则：");
                    egui::ScrollArea::vertical().max_height(110.0).show(ui, |ui| {
                        for (source, rule) in self.settings.filters.active_rules() {
                            ui.monospace(format!("{source}: {rule}"));
                        }
                    });
                    if ui.button("保存排除规则").clicked() { self.save(); self.refresh(); }
                });
            }
        });
    }
}
fn main() -> eframe::Result<()> {
    let options = eframe::NativeOptions { viewport: egui::ViewportBuilder::default().with_inner_size([850.0, 720.0]), ..Default::default() };
    eframe::run_native("OfficeGitSync", options, Box::new(|cc| {
        let mut fonts = egui::FontDefinitions::default();
        #[cfg(target_os = "windows")]
        {
            let candidates = [r"C:\Windows\Fonts\msyh.ttc", r"C:\Windows\Fonts\simsun.ttc", r"C:\Windows\Fonts\simhei.ttf"];
            if let Some(path) = candidates.iter().find(|p| std::path::Path::new(p).exists()) {
                if let Ok(data) = fs::read(path) {
                    fonts.font_data.insert("chinese".into(), egui::FontData::from_owned(data).into());
                    for family in [egui::FontFamily::Proportional, egui::FontFamily::Monospace] {
                        fonts.families.entry(family).or_default().insert(0, "chinese".into());
                    }
                }
            }
        }
        cc.egui_ctx.set_fonts(fonts);
        Ok(Box::new(App::default()))
    }))
}
