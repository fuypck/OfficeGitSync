mod filters;
use eframe::egui;
use git2::{Repository, StatusOptions};
use std::path::PathBuf;

struct App {
    repo_path: String,
    message: String,
    status: Vec<String>,
    error: String,
    advanced: bool,
    filters: filters::FilterSettings,
}

impl Default for App {
    fn default() -> Self {
        Self {
            repo_path: std::env::current_dir().unwrap_or_else(|_| PathBuf::from(".")).display().to_string(),
            message: String::new(),
            status: Vec::new(),
            error: String::new(),
            advanced: false,
            filters: filters::FilterSettings::default(),
        }
    }
}

impl App {
    fn refresh(&mut self) {
        self.status.clear();
        self.error.clear();
        let result = (|| -> Result<(), git2::Error> {
            let repo = Repository::open(&self.repo_path)?;
            let mut opts = StatusOptions::new();
            opts.include_untracked(true).recurse_untracked_dirs(true);
            for entry in repo.statuses(Some(&mut opts))?.iter() {
                let path = entry.path().unwrap_or("<unknown>");
                if !self.filters.is_excluded(path) {
                    self.status.push(format!("{:?}  {}", entry.status(), path));
                }
            }
            Ok(())
        })();
        if let Err(err) = result { self.error = err.to_string(); }
    }
}

impl eframe::App for App {
    fn update(&mut self, ctx: &egui::Context, _: &mut eframe::Frame) {
        egui::CentralPanel::default().show(ctx, |ui| {
            ui.heading("OfficeGitSync — Rust preview");
            ui.horizontal(|ui| {
                ui.selectable_value(&mut self.advanced, false, "Document backup");
                ui.selectable_value(&mut self.advanced, true, "Advanced Git");
            });
            ui.horizontal(|ui| {
                ui.label("Repository:");
                ui.text_edit_singleline(&mut self.repo_path);
                if ui.button("Refresh status").clicked() { self.refresh(); }
            });
            if !self.error.is_empty() { ui.colored_label(egui::Color32::RED, &self.error); }
            ui.separator();
            if self.advanced {
                ui.heading("Git status");
            } else {
                ui.heading("Document changes");
                ui.label("Automatic watching and commits are not implemented in this first scaffold.");
            }
            egui::ScrollArea::vertical().max_height(250.0).show(ui, |ui| {
                for entry in &self.status { ui.label(entry); }
            });
            ui.separator();
            ui.collapsing("Temporary-file exclusions", |ui| {
                for preset in &mut self.filters.presets {
                    ui.checkbox(&mut preset.enabled, &preset.name);
                }
                ui.label("Custom patterns (one per line):");
                ui.text_edit_multiline(&mut self.message);
                if ui.button("Apply custom patterns").clicked() {
                    self.filters.custom = self.message.lines().map(str::trim).filter(|s| !s.is_empty()).map(str::to_owned).collect();
                }
            });
        });
    }
}

fn main() -> eframe::Result<()> {
    let options = eframe::NativeOptions::default();
    eframe::run_native("OfficeGitSync", options, Box::new(|_| Ok(Box::new(App::default()))))
}
