use super::*;

#[derive(Clone, Copy, PartialEq, Eq)]
pub(super) enum Page {
    Backup,
    History,
    Settings,
    Advanced,
}

#[derive(Clone, Copy, PartialEq, Eq)]
pub(super) enum SettingsSection {
    General,
    Filters,
    Import,
}

#[derive(Clone, Copy, PartialEq, Eq)]
pub(super) enum AdvancedSection {
    Workspace,
    Remote,
}

impl eframe::App for App {
    fn update(&mut self, ctx: &egui::Context, frame: &mut eframe::Frame) {
        self.update_desktop(ctx);
        if self.quitting {
            return;
        }
        self.update_monitor(ctx);

        egui::TopBottomPanel::top("navigation").show(ctx, |ui| {
            ui.add_space(6.0);
            ui.horizontal(|ui| {
                ui.heading("OfficeGitSync");
                ui.weak(env!("OFFICEGITSYNC_VERSION"));
            });
            ui.add_space(6.0);
            ui.horizontal(|ui| {
                let backup_label = if self.pending_backup && self.page != Page::Backup {
                    "文档备份（待确认）"
                } else {
                    "文档备份"
                };
                let previous = self.page;
                for (page, label) in [
                    (Page::Backup, backup_label),
                    (Page::History, "历史版本"),
                    (Page::Settings, "设置"),
                    (Page::Advanced, "Git 高级"),
                ] {
                    ui.selectable_value(&mut self.page, page, label);
                }
                if previous != self.page {
                    self.restore_confirm = false;
                    if self.page == Page::History {
                        self.read_history();
                    }
                }
            });
            ui.add_space(5.0);
        });

        if !self.notice.is_empty() {
            egui::TopBottomPanel::bottom("status").show(ctx, |ui| {
                ui.add_space(3.0);
                ui.add(egui::Label::new(&self.notice).truncate())
                    .on_hover_text(&self.notice);
                ui.add_space(3.0);
            });
        }

        egui::CentralPanel::default().show(ctx, |ui| match self.page {
            Page::Backup => self.show_backup(ui, frame),
            Page::History => self.show_history(ui, frame),
            Page::Settings => self.show_settings(ui, ctx),
            Page::Advanced => self.show_advanced(ui),
        });
    }
}

impl App {
    pub(super) fn apply_theme(&self, ctx: &egui::Context) {
        use settings::Theme;
        ctx.set_theme(match self.settings.theme {
            Theme::System => egui::ThemePreference::System,
            Theme::Light => egui::ThemePreference::Light,
            Theme::Dark => egui::ThemePreference::Dark,
        });
    }

    fn show_backup(&mut self, ui: &mut egui::Ui, _frame: &eframe::Frame) {
        ui.label(egui::RichText::new("工作目录").strong());
        ui.horizontal(|ui| {
            if ui
                .add(
                    egui::TextEdit::singleline(&mut self.settings.repo_path)
                        .desired_width((ui.available_width() - 75.0).max(180.0)),
                )
                .changed()
            {
                self.reset_worktree_view();
            }
            #[cfg(target_os = "windows")]
            if ui.button("浏览…").clicked() {
                self.browse_worktree(_frame);
            }
        });
        ui.horizontal_wrapped(|ui| {
            ui.checkbox(&mut self.settings.auto_check, "监听工作目录");
            ui.weak(if self.settings.auto_check && self.monitor.is_some() {
                "监听中"
            } else {
                "未监听"
            });
            if self.pending_backup {
                ui.label(egui::RichText::new("有变更待确认").strong());
                if ui.button("暂不备份").clicked() {
                    self.clear_changes();
                    self.backup_tag.clear();
                    self.message_edited = false;
                    self.prefill_message();
                    self.notice = "已取消本轮备份，仍可手动扫描这些变更".into();
                }
            }
            ui.separator();
            if ui.button("扫描变更").clicked() {
                self.refresh();
            }
            if ui.button("初始化 Git 仓库").clicked() {
                self.init_repo();
            }
        });
        ui.separator();

        let before = self.changes.iter().filter(|change| change.selected).count();
        ui.horizontal(|ui| {
            ui.heading("文件变更");
            ui.weak(format!("{} 项", self.changes.len()));
            if !self.changes.is_empty() {
                let all_selected = before == self.changes.len();
                let label = if all_selected {
                    "取消全选"
                } else {
                    "全选"
                };
                if ui.button(label).clicked() {
                    for change in &mut self.changes {
                        change.selected = !all_selected;
                    }
                }
            }
        });
        let has_tag = !self.backup_tag.trim().is_empty();
        let footer_height = if has_tag { 104.0 } else { 82.0 };
        let list_height = (ui.available_height() - footer_height).max(86.0);
        egui::Frame::group(ui.style()).show(ui, |ui| {
            let area = egui::ScrollArea::vertical()
                .id_salt("backup_files")
                .max_height(list_height)
                .auto_shrink([false, false]);
            if self.changes.is_empty() {
                area.show(ui, |ui| {
                    ui.weak("没有待备份的文件");
                });
            } else {
                let row_height = ui.spacing().interact_size.y;
                area.show_rows(ui, row_height, self.changes.len(), |ui, range| {
                    for change in &mut self.changes[range] {
                        ui.horizontal(|ui| {
                            ui.checkbox(&mut change.selected, "");
                            let width = (ui.available_width() - 86.0).max(20.0);
                            ui.allocate_ui(egui::vec2(width, row_height), |ui| {
                                ui.add(egui::Label::new(&change.path).truncate())
                                    .on_hover_text(&change.path);
                            });
                            ui.small(&change.status);
                        });
                    }
                });
            }
        });
        ui.add_space(8.0);
        let count = self.changes.iter().filter(|change| change.selected).count();
        if before == 0 && count > 0 {
            self.prefill_message();
        }
        ui.label(egui::RichText::new("提交说明").strong());
        ui.horizontal(|ui| {
            let width = (ui.available_width() - 136.0).max(160.0);
            let response =
                ui.add(egui::TextEdit::singleline(&mut self.message).desired_width(width));
            self.message_edited |= response.changed();
            let button = primary_button(ui, format!("确认备份（{count}）"));
            if ui.add_enabled(count > 0, button).clicked() {
                self.commit_selected();
            }
        });
        if has_tag {
            ui.weak(format!("版本标签  {}", self.backup_tag.trim()));
        }
    }

    fn show_settings(&mut self, ui: &mut egui::Ui, ctx: &egui::Context) {
        ui.horizontal(|ui| {
            for (section, label) in [
                (SettingsSection::General, "常规"),
                (SettingsSection::Filters, "排除规则"),
                (SettingsSection::Import, "导入配置"),
            ] {
                ui.selectable_value(&mut self.settings_section, section, label);
            }
        });
        ui.separator();
        match self.settings_section {
            SettingsSection::General => self.show_general_settings(ui, ctx),
            SettingsSection::Filters => self.show_filter_editor(ui),
            SettingsSection::Import => {
                ui.heading("导入 Python 版配置");
                ui.label("选择旧版 .officegitsync.json 文件。导入会更新当前工作目录、监听选项和排除规则。");
                ui.horizontal(|ui| {
                    ui.text_edit_singleline(&mut self.python_config_path);
                    if ui.button("导入并保存").clicked() {
                        self.import_python_settings();
                    }
                });
            }
        }
    }

    fn show_general_settings(&mut self, ui: &mut egui::Ui, ctx: &egui::Context) {
        ui.heading("外观");
        ui.horizontal(|ui| {
            use settings::Theme;
            let previous = self.settings.theme;
            for (theme, label) in [
                (Theme::System, "跟随系统"),
                (Theme::Light, "浅色"),
                (Theme::Dark, "深色"),
            ] {
                ui.radio_value(&mut self.settings.theme, theme, label);
            }
            if previous != self.settings.theme {
                self.apply_theme(ctx);
                self.notice = match settings::remember_theme(self.settings.theme) {
                    Ok(()) => "主题已保存".into(),
                    Err(e) => format!("主题已应用，保存失败：{e}"),
                };
            }
        });
        ui.add_space(12.0);
        ui.heading("监听");
        ui.horizontal(|ui| {
            ui.label("防抖时间（秒）");
            ui.add(egui::DragValue::new(&mut self.settings.interval_secs).range(1..=3600));
        });
        ui.checkbox(&mut self.settings.enable_log, "记录备份和监听日志");
        ui.add_space(12.0);
        ui.heading("后台运行");
        ui.checkbox(&mut self.settings.close_to_tray, "关闭窗口时隐藏到托盘");
        ui.checkbox(
            &mut self.settings.silent_start,
            "启动时隐藏到托盘（需已开启监听）",
        );
        let mut enabled = self.autostart;
        if ui
            .checkbox(&mut enabled, "登录时启动 OfficeGitSync")
            .changed()
        {
            match autostart::set(enabled) {
                Ok(()) => self.autostart = enabled,
                Err(e) => self.notice = format!("设置开机启动失败：{e}"),
            }
        }
        let tray_available = self
            .desktop
            .as_ref()
            .is_some_and(desktop::Desktop::available);
        if !tray_available {
            ui.weak("系统托盘不可用，关闭窗口将退出程序。");
        }
        ui.add_space(12.0);
        ui.horizontal(|ui| {
            if ui.add(primary_button(ui, "保存设置")).clicked() {
                self.save();
            }
            if ui
                .add_enabled(tray_available, egui::Button::new("隐藏到托盘"))
                .clicked()
            {
                self.hide(ctx);
            }
            if ui.button("退出程序").clicked() {
                self.quit(ctx);
            }
        });
    }

    fn show_advanced(&mut self, ui: &mut egui::Ui) {
        ui.horizontal(|ui| {
            ui.selectable_value(
                &mut self.advanced_section,
                AdvancedSection::Workspace,
                "差异与标签",
            );
            ui.selectable_value(
                &mut self.advanced_section,
                AdvancedSection::Remote,
                "远程仓库",
            );
        });
        ui.separator();
        match self.advanced_section {
            AdvancedSection::Workspace => {
                ui.horizontal(|ui| {
                    ui.label("下次备份的版本标签（可选）");
                    ui.text_edit_singleline(&mut self.backup_tag);
                });
                ui.heading("文件差异");
                egui::ScrollArea::vertical()
                    .id_salt("diff_files")
                    .max_height(160.0)
                    .show(ui, |ui| {
                        let mut selected = None;
                        for change in &self.changes {
                            if ui
                                .selectable_label(self.selected_path == change.path, &change.path)
                                .clicked()
                            {
                                selected = Some(change.path.clone());
                            }
                        }
                        if let Some(path) = selected {
                            self.show_diff(&path);
                        }
                    });
                if !self.selected_path.is_empty() {
                    ui.label(&self.selected_path);
                    egui::ScrollArea::both()
                        .id_salt("diff_text")
                        .show(ui, |ui| {
                            ui.add(
                                egui::TextEdit::multiline(&mut self.diff.as_str())
                                    .font(egui::TextStyle::Monospace)
                                    .desired_width(ui.available_width()),
                            );
                        });
                }
            }
            AdvancedSection::Remote => {
                ui.heading("远程仓库");
                ui.label("Remote 名称");
                ui.text_edit_singleline(&mut self.settings.remote_name);
                ui.label("Git URL / 本地目录 / 局域网共享路径");
                ui.text_edit_singleline(&mut self.settings.remote_url);
                ui.horizontal(|ui| {
                    if ui.button("添加 Remote").clicked() {
                        self.remote_action("add");
                    }
                    if ui.button("Fetch").clicked() {
                        self.remote_action("fetch");
                    }
                    if ui.button("保存配置").clicked() {
                        self.save();
                    }
                });
                ui.weak("复杂 HTTPS / SSH 认证与 Push/Pull 尚待完善。");
            }
        }
    }
}

fn primary_button(ui: &egui::Ui, label: impl Into<String>) -> egui::Button<'static> {
    let accent = if ui.visuals().dark_mode {
        egui::Color32::from_rgb(39, 102, 176)
    } else {
        egui::Color32::from_rgb(25, 81, 148)
    };
    egui::Button::new(
        egui::RichText::new(label)
            .strong()
            .color(egui::Color32::WHITE),
    )
    .fill(accent)
    .min_size(egui::vec2(110.0, 30.0))
}
impl App {
    fn show_history(&mut self, ui: &mut egui::Ui, _frame: &eframe::Frame) {
        ui.horizontal(|ui| {
            ui.heading("提交历史");
            if ui.button("刷新历史").clicked() {
                self.read_history();
            }
        });
        let row_height = ui.spacing().interact_size.y;
        let history_height = (ui.available_height() * 0.49).max(100.0);
        egui::ScrollArea::vertical()
            .id_salt("history_list")
            .max_height(history_height)
            .show_rows(ui, row_height, self.history.len(), |ui, range| {
                for version in &self.history[range] {
                    let id = version.id.to_string();
                    let current = if version.current { "（当前）" } else { "" };
                    let summary = version.message.lines().next().unwrap_or("（无说明）");
                    let row = format!(
                        "{}{current}  {}  {}  {summary}",
                        &id[..8],
                        version.date,
                        version.tags.join(", ")
                    );
                    if ui
                        .add(
                            egui::Button::new(&row)
                                .selected(self.history_selected == Some(version.id))
                                .truncate(),
                        )
                        .on_hover_text(row)
                        .clicked()
                    {
                        self.history_selected = Some(version.id);
                        self.restore_confirm = false;
                    }
                }
            });
        let Some(id) = self.history_selected else {
            return;
        };
        if let Some(version) = self.history.iter().find(|version| version.id == id) {
            egui::ScrollArea::vertical()
                .id_salt("commit_message")
                .max_height(60.0)
                .show(ui, |ui| {
                    ui.label(&version.message);
                });
        }
        ui.horizontal(|ui| {
            ui.label("标签名");
            ui.text_edit_singleline(&mut self.tag_name);
            if ui.button("创建标签").clicked() {
                match self
                    .repo()
                    .and_then(|repo| history::tag(&repo, id, &self.tag_name))
                {
                    Ok(()) => {
                        self.notice = "已创建标签".into();
                        self.tag_name.clear();
                        self.read_history();
                    }
                    Err(e) => self.notice = format!("创建标签失败：{e}"),
                }
            }
        });
        ui.separator();
        ui.label(egui::RichText::new("导出到目标目录下的新子目录").strong());
        ui.horizontal(|ui| {
            if ui
                .add(
                    egui::TextEdit::singleline(&mut self.restore_parent)
                        .desired_width((ui.available_width() - 185.0).max(140.0)),
                )
                .changed()
            {
                self.restore_confirm = false;
            }
            #[cfg(target_os = "windows")]
            if ui.button("浏览…").clicked() {
                let result = crate::windows_ui::hwnd(_frame).and_then(|owner| {
                    crate::windows_ui::pick_folder(owner, "选择导出目录").map_err(|e| e.to_string())
                });
                match result {
                    Ok(Some(path)) => match path.into_os_string().into_string() {
                        Ok(path) => {
                            self.restore_parent = path;
                            self.restore_confirm = false;
                        }
                        Err(_) => self.notice = "所选目录的名称无法用 Unicode 表示".into(),
                    },
                    Ok(None) => {}
                    Err(e) => self.notice = format!("选择导出目录失败：{e}"),
                }
            }
            if ui.button("准备导出").clicked() {
                self.restore_confirm = true;
            }
        });
        if self.restore_confirm {
            ui.label(format!(
                "将版本 {} 的内容导出到独立子目录，当前文档和提交历史将保留。",
                &id.to_string()[..8]
            ));
            ui.horizontal(|ui| {
                if ui.add(primary_button(ui, "确认导出")).clicked() {
                    let destination = std::path::Path::new(self.restore_parent.trim());
                    let result = if destination.is_absolute() {
                        self.repo()
                            .and_then(|repo| history::restore(&repo, id, destination))
                    } else {
                        Err("请输入已存在的绝对路径".into())
                    };
                    self.notice = match result {
                        Ok(path) => {
                            let parent = self.restore_parent.trim();
                            self.settings.last_export_parent = parent.to_owned();
                            let mut message = format!("版本已导出到 {}", path.display());
                            if let Err(e) = settings::remember_export_parent(parent) {
                                message.push_str(&format!("，记住导出目录失败：{e}"));
                            }
                            message
                        }
                        Err(e) => format!("恢复失败：{e}"),
                    };
                    self.restore_confirm = false;
                }
                if ui.button("取消").clicked() {
                    self.restore_confirm = false;
                }
            });
        }
    }
}

impl App {
    fn show_filter_editor(&mut self, ui: &mut egui::Ui) {
        let height = (ui.available_height() - 48.0).max(100.0);
        egui::ScrollArea::vertical()
            .id_salt("filter_editor")
            .max_height(height)
            .show(ui, |ui| {
                ui.label("Git 元数据目录 .git 始终排除");
                for (preset_index, preset) in self.filter_editor.presets.iter_mut().enumerate() {
                    ui.push_id(preset_index, |ui| {
                        ui.checkbox(&mut preset.enabled, &preset.name);
                        if preset.enabled {
                            ui.indent("规则", |ui| {
                                let mut remove = None;
                                for (index, pattern) in preset.patterns.iter_mut().enumerate() {
                                    ui.push_id(index, |ui| {
                                        ui.horizontal(|ui| {
                                            ui.text_edit_singleline(pattern);
                                            if ui.button("删除").clicked() {
                                                remove = Some(index);
                                            }
                                        });
                                    });
                                }
                                if let Some(index) = remove {
                                    preset.patterns.remove(index);
                                }
                                if ui.button("添加规则").clicked() {
                                    preset.patterns.push(String::new());
                                }
                            });
                        }
                    });
                }
                ui.label("自定义规则（每行一条）");
                ui.add(egui::TextEdit::multiline(&mut self.custom_editor).desired_rows(3));
                ui.separator();
                ui.heading("当前生效的规则");
                for (source, rule) in self.settings.filters.active_rules() {
                    ui.monospace(format!("{source}  {rule}"));
                }
            });
        ui.add_space(8.0);
        ui.horizontal(|ui| {
            if ui.button("保存排除规则").clicked() {
                self.save_filters();
            }
            if ui.button("恢复默认规则").clicked() {
                self.filter_editor = filters::FilterSettings::default();
                self.custom_editor.clear();
            }
            if ui.button("撤销未保存的编辑").clicked() {
                self.reset_filter_editor();
            }
        });
    }
}
