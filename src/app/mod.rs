mod ui;

use crate::{autostart, backup, desktop, filters, history, instance, monitor, remote, settings};
use backup::Change;
use eframe::egui;
use git2::{DiffOptions, Repository, RepositoryInitOptions};
use settings::Settings;
use std::{path::Path, time::Duration};
use ui::{AdvancedSection, Page, SettingsSection};

#[cfg(any(target_os = "linux", target_os = "windows"))]
type PickerResult = Result<Option<std::path::PathBuf>, String>;

#[derive(Clone, Copy)]
#[cfg(target_os = "linux")]
enum PickerPurpose {
    PythonConfig,
    RemoteFolder,
}

pub(crate) struct App {
    settings: Settings,
    changes: Vec<Change>,
    reviewed: Option<backup::Revision>,
    history: Vec<history::Version>,
    history_selected: Option<git2::Oid>,
    restore_parent: String,
    restore_confirm: bool,
    tag_name: String,
    message: String,
    message_edited: bool,
    backup_tag: String,
    diff: String,
    selected_path: String,
    notice: String,
    page: Page,
    settings_section: SettingsSection,
    advanced_section: AdvancedSection,
    filter_editor: filters::FilterSettings,
    custom_editor: String,
    python_config_path: String,
    python_import_error: Option<String>,
    #[cfg(target_os = "linux")]
    picker: Option<(PickerPurpose, std::sync::mpsc::Receiver<PickerResult>)>,
    remote_init: Option<std::path::PathBuf>,
    monitor: Option<monitor::Monitor>,
    pending_backup: bool,
    desktop: Option<desktop::Desktop>,
    instance: instance::Instance,
    hidden: bool,
    quitting: bool,
    autostart: bool,
}
impl App {
    pub(crate) fn new(cc: &eframe::CreationContext<'_>, instance: instance::Instance) -> Self {
        let ctx = &cc.egui_ctx;
        let loaded = settings::load();
        let settings = loaded.as_ref().cloned().unwrap_or_default();
        let restore_parent = settings.last_export_parent.clone();
        let filter_editor = settings.filters.clone();
        let custom_editor = settings.filters.custom.join("\n");
        let desktop = desktop::Desktop::start(cc);
        let autostart = autostart::enabled();
        let mut app = Self {
            settings,
            changes: Vec::new(),
            reviewed: None,
            history: Vec::new(),
            history_selected: None,
            restore_parent,
            restore_confirm: false,
            tag_name: String::new(),
            message: String::new(),
            message_edited: false,
            backup_tag: String::new(),
            diff: String::new(),
            selected_path: String::new(),
            notice: String::new(),
            page: Page::Backup,
            settings_section: SettingsSection::General,
            advanced_section: AdvancedSection::Workspace,
            filter_editor,
            custom_editor,
            python_config_path: String::new(),
            python_import_error: None,
            #[cfg(target_os = "linux")]
            picker: None,
            remote_init: None,
            monitor: None,
            pending_backup: false,
            desktop: None,
            instance,
            hidden: false,
            quitting: false,
            autostart: autostart.as_ref().copied().unwrap_or(false),
        };
        app.apply_theme(ctx);
        app.refresh();
        match desktop {
            Ok(desktop) => {
                app.desktop = Some(desktop);
                if app.settings.silent_start && app.settings.auto_check {
                    app.update_monitor(ctx);
                    if app.monitor.is_some() {
                        app.hide(ctx);
                    }
                }
            }
            Err(e) => app.notice = format!("系统托盘不可用，关闭窗口将退出：{e}"),
        }
        if let Err(e) = autostart {
            app.notice = format!("读取开机启动设置失败：{e}");
        }
        if let Err(e) = loaded {
            app.notice = e;
        }
        app
    }
    fn hide(&mut self, ctx: &egui::Context) {
        self.hidden = true;
        ctx.send_viewport_cmd(egui::ViewportCommand::Visible(false));
    }

    fn show(&mut self, ctx: &egui::Context) {
        self.hidden = false;
        ctx.send_viewport_cmd(egui::ViewportCommand::Visible(true));
        ctx.send_viewport_cmd(egui::ViewportCommand::Minimized(false));
        ctx.send_viewport_cmd(egui::ViewportCommand::Focus);
    }

    fn quit(&mut self, ctx: &egui::Context) {
        self.quitting = true;
        self.monitor = None;
        ctx.send_viewport_cmd(egui::ViewportCommand::Close);
    }

    fn update_desktop(&mut self, ctx: &egui::Context) {
        ctx.request_repaint_after(Duration::from_millis(500));
        if self.instance.take_show_request() {
            self.show(ctx);
        }
        let actions: Vec<_> = self
            .desktop
            .as_ref()
            .into_iter()
            .flat_map(desktop::Desktop::actions)
            .collect();
        for action in actions {
            match action {
                desktop::Action::Show => self.show(ctx),
                desktop::Action::Quit => self.quit(ctx),
                #[cfg(target_os = "linux")]
                desktop::Action::TrayUnavailable => {
                    self.desktop = None;
                    if self.hidden {
                        self.show(ctx);
                    }
                    self.notice = "系统托盘已断开，关闭窗口将退出".into();
                }
            }
        }
        if self.quitting {
            return;
        }
        if ctx.input(|input| input.viewport().close_requested())
            && self.settings.close_to_tray
            && self
                .desktop
                .as_ref()
                .is_some_and(desktop::Desktop::available)
        {
            ctx.send_viewport_cmd(egui::ViewportCommand::CancelClose);
            self.hide(ctx);
        }
    }

    #[cfg(target_os = "windows")]
    fn browse_worktree(&mut self, frame: &eframe::Frame) {
        let result = crate::windows_ui::hwnd(frame).and_then(|owner| {
            crate::windows_ui::pick_folder(owner, "选择工作目录").map_err(|e| e.to_string())
        });
        match result {
            Ok(Some(path)) => {
                let Ok(path) = path.into_os_string().into_string() else {
                    self.notice = "所选目录的名称无法用 Unicode 表示".into();
                    return;
                };
                if self.settings.repo_path != path {
                    self.reset_worktree_view();
                    self.settings.repo_path = path;
                    self.refresh();
                }
            }
            Ok(None) => {}
            Err(e) => self.notice = format!("选择工作目录失败：{e}"),
        }
    }

    fn worktree_path(&self) -> Result<&Path, String> {
        let path = Path::new(&self.settings.repo_path);
        if self.settings.repo_path.trim().is_empty() || !path.is_dir() {
            return Err("请输入已存在的工作目录".into());
        }
        Ok(path)
    }

    fn repo(&self) -> Result<Repository, String> {
        Repository::open(self.worktree_path()?).map_err(|e| e.to_string())
    }
    fn prefill_message(&mut self) {
        if !self.message_edited {
            self.message = chrono::Local::now().format("%Y-%m-%d %H:%M:%S").to_string();
        }
    }

    fn refresh(&mut self) {
        self.clear_changes();
        self.prefill_message();
        match self.read_status() {
            Ok((reviewed, changes)) => {
                self.reviewed = Some(reviewed);
                self.changes = changes;
                self.notice = format!("已刷新，共 {} 项变更", self.changes.len());
            }
            Err(e) => {
                self.notice = format!("仓库读取失败：{e}");
            }
        }
        self.read_history();
    }
    fn read_status(&self) -> Result<(backup::Revision, Vec<Change>), String> {
        backup::scan(&self.repo()?, &self.settings.filters)
    }
    fn update_monitor(&mut self, ctx: &egui::Context) {
        if !self.settings.auto_check {
            self.monitor = None;
            return;
        }
        if self.monitor.is_none() {
            let result = self.repo().and_then(|repo| {
                let workdir = repo.workdir().ok_or("裸仓库没有可监听的工作区")?;
                let root = workdir.canonicalize().map_err(|e| e.to_string())?;
                monitor::Monitor::start(&root, self.settings.filters.compile()?, ctx)
            });
            match result {
                Ok(monitor) => {
                    self.monitor = Some(monitor);
                    self.notice = "正在监听工作目录，备份时需要人工确认".into();
                }
                Err(e) => {
                    self.settings.auto_check = false;
                    self.report(format!("无法启动监听：{e}"));
                }
            }
        }
        let Some(monitor) = &mut self.monitor else {
            return;
        };
        let debounce = Duration::from_secs(self.settings.interval_secs.clamp(1, 3600));
        match monitor.collect(debounce, ctx) {
            Ok(Some(paths)) => match self.read_status() {
                Ok((reviewed, mut changes)) => {
                    let same_revision = self.reviewed.as_ref() == Some(&reviewed);
                    let previous = if self.pending_backup && same_revision {
                        self.changes.as_slice()
                    } else {
                        &[]
                    };
                    changes.retain_mut(|change| {
                        let old = previous.iter().find(|old| old.path == change.path);
                        let affected = Path::new(&change.path)
                            .ancestors()
                            .any(|path| paths.contains(path));
                        change.selected = !affected && old.is_some_and(|old| old.selected);
                        old.is_some() || affected
                    });
                    if !changes.is_empty() {
                        if !self.pending_backup || !same_revision {
                            self.prefill_message();
                        }
                        self.reviewed = Some(reviewed);
                        self.changes = changes;
                        self.pending_backup = true;
                        self.selected_path.clear();
                        if self.hidden
                            || ctx.input(|input| input.viewport().minimized == Some(true))
                        {
                            self.page = Page::Backup;
                            self.restore_confirm = false;
                            self.show(ctx);
                        }
                        self.notice = "检测到文件变化，请勾选要备份的文件并确认".into();
                    } else if self.pending_backup || !same_revision {
                        self.clear_changes();
                        self.notice = "待备份文件已无 Git 变更".into();
                    }
                }
                Err(e) => {
                    self.clear_changes();
                    if self.hidden {
                        self.show(ctx);
                    }
                    self.report(format!("监听到变更，但读取 Git 状态失败：{e}"));
                }
            },
            Ok(None) => {}
            Err(e) => {
                self.monitor = None;
                self.settings.auto_check = false;
                if self.hidden {
                    self.show(ctx);
                }
                self.report(format!("文件监听已停止：{e}"));
            }
        }
    }
    fn read_history(&mut self) {
        self.history.clear();
        if let Ok(repo) = self.repo() {
            match history::versions(&repo) {
                Ok(versions) => self.history = versions,
                Err(e) => self.notice = format!("加载历史失败：{e}"),
            }
        }
        if !self
            .history
            .iter()
            .any(|version| Some(version.id) == self.history_selected)
        {
            self.history_selected = None;
            self.restore_confirm = false;
        }
    }

    fn init_repo(&mut self) {
        let mut opts = RepositoryInitOptions::new();
        opts.no_reinit(true).mkdir(false).mkpath(false);
        match self
            .worktree_path()
            .and_then(|path| Repository::init_opts(path, &opts).map_err(|e| e.to_string()))
        {
            Ok(_) => self.refresh(),
            Err(e) => self.notice = format!("初始化失败：{e}"),
        }
    }
    fn commit_selected(&mut self) {
        let Some(reviewed) = &self.reviewed else {
            self.notice = "请扫描工作目录并选择要备份的文件".into();
            return;
        };
        let selected = self
            .changes
            .iter()
            .filter(|change| change.selected)
            .map(|change| change.path.clone())
            .collect();
        let tag_name = self.backup_tag.trim();
        let result = self.repo().and_then(|repo| {
            if !tag_name.is_empty() {
                history::check_tag_name(&repo, tag_name)?;
            }
            let id = backup::commit(
                &repo,
                reviewed,
                &self.settings.filters,
                &selected,
                &self.message,
            )?;
            let tag_result = (!tag_name.is_empty()).then(|| history::tag(&repo, id, tag_name));
            Ok((id, tag_result))
        });
        match result {
            Ok((id, tag_result)) => {
                self.message_edited = false;
                self.refresh();
                let id = id.to_string();
                let mut message = format!("提交成功：{}", &id[..8]);
                match tag_result {
                    Some(Ok(())) => {
                        message.push_str(&format!("，已创建标签 {}", self.backup_tag.trim()));
                        self.backup_tag.clear();
                    }
                    Some(Err(e)) => message.push_str(&format!("，标签创建失败：{e}")),
                    None => {}
                }
                self.report(message);
            }
            Err(e) => self.report(format!("提交失败：{e}")),
        }
    }
    fn show_diff(&mut self, path: &str) {
        let result = (|| -> Result<String, String> {
            let repo = self.repo()?;
            let mut opts = DiffOptions::new();
            opts.pathspec(path).disable_pathspec_match(true);
            let diff = repo
                .diff_index_to_workdir(None, Some(&mut opts))
                .map_err(|e| e.to_string())?;
            let mut out = String::new();
            diff.print(git2::DiffFormat::Patch, |_delta, _hunk, line| {
                out.push(line.origin());
                out.push_str(&String::from_utf8_lossy(line.content()));
                true
            })
            .map_err(|e| e.to_string())?;
            if out.is_empty() {
                out = "无文本差异（Office 二进制文件暂不支持语义 Diff，或文件尚未加入索引）".into();
            }
            Ok(out)
        })();
        self.diff = result.unwrap_or_else(|e| format!("Diff 失败：{e}"));
        self.selected_path = path.into();
    }
    fn save_remote(&mut self) {
        self.remote_init = None;
        let result = self.repo().and_then(|repo| {
            let name = self.settings.remote_name.trim();
            if !git2::Remote::is_valid_name(name) {
                return Err("远程仓库名称无效".into());
            }
            match remote::inspect(&repo, &self.settings.remote_url)? {
                remote::Target::Empty(path) => {
                    self.remote_init = Some(path);
                    Ok("目标是空目录，等待确认初始化裸仓库")
                }
                target => {
                    remote::save(&repo, name, &target)?;
                    Ok(match target {
                        remote::Target::Bare(_) => "已保存裸仓库，可 Fetch 或推送当前分支",
                        remote::Target::Worktree(_) => "已保存带工作区的仓库，可 Fetch",
                        _ => "已保存远程地址",
                    })
                }
            }
        });
        self.notice = result
            .map(str::to_owned)
            .unwrap_or_else(|e| format!("远程配置失败：{e}"));
    }

    fn initialize_remote(&mut self) {
        let Some(path) = self.remote_init.take() else {
            return;
        };
        self.notice = match self.repo().and_then(|repo| {
            remote::initialize(
                &repo,
                self.settings.remote_name.trim(),
                &self.settings.remote_url,
                &path,
            )
        }) {
            Ok(()) => "裸仓库已初始化并登记，可推送当前分支".into(),
            Err(e) => format!("初始化远程失败：{e}"),
        };
    }

    fn fetch_remote(&mut self) {
        self.notice = match self.repo().and_then(|repo| {
            remote::fetch(
                &repo,
                self.settings.remote_name.trim(),
                &self.settings.remote_url,
            )
        }) {
            Ok(()) => "Fetch 完成".into(),
            Err(e) => format!("Fetch 失败：{e}"),
        };
    }

    fn push_local_remote(&mut self) {
        self.notice = match self.repo().and_then(|repo| {
            remote::push_local(
                &repo,
                self.settings.remote_name.trim(),
                &self.settings.remote_url,
            )
        }) {
            Ok(()) => "当前分支已推送到裸仓库".into(),
            Err(e) => format!("推送失败：{e}"),
        };
    }

    #[cfg(any(target_os = "linux", target_os = "windows"))]
    fn apply_remote_picker_result(&mut self, result: PickerResult) {
        match result {
            Ok(Some(path)) => match path.into_os_string().into_string() {
                Ok(path) => {
                    self.settings.remote_url = path;
                    self.remote_init = None;
                }
                Err(_) => self.notice = "文件夹路径不是有效的 Unicode".into(),
            },
            Ok(None) => {}
            Err(e) => self.notice = format!("无法选择远程目录：{e}"),
        }
    }

    fn report(&mut self, message: String) {
        self.notice = message;
        if self.settings.enable_log {
            if let Err(e) = settings::append_log(&self.notice) {
                self.notice.push_str(&format!("（日志写入失败：{e}）"));
            }
        }
    }

    fn save(&mut self) {
        self.notice = match settings::save(&self.settings) {
            Ok(()) => "设置已保存".into(),
            Err(e) => format!("保存失败：{e}"),
        };
    }

    fn save_filters(&mut self) {
        let mut next = self.settings.clone();
        next.filters = self.filter_editor.clone();
        next.filters.custom = self.custom_editor.lines().map(str::to_owned).collect();
        next.filters.normalize();
        let result = next.filters.compile().and_then(|matcher| {
            settings::save(&next)?;
            if let Some(monitor) = &mut self.monitor {
                monitor.set_filters(matcher);
            }
            Ok(())
        });
        match result {
            Ok(()) => {
                self.settings = next;
                self.reset_filter_editor();
                self.refresh();
                self.notice = "排除规则已保存并生效".into();
            }
            Err(e) => self.notice = format!("保存排除规则失败：{e}"),
        }
    }

    fn reset_filter_editor(&mut self) {
        self.filter_editor = self.settings.filters.clone();
        self.custom_editor = self.filter_editor.custom.join("\n");
    }

    #[cfg(any(target_os = "linux", target_os = "windows"))]
    fn apply_python_picker_result(&mut self, result: PickerResult) {
        match result {
            Ok(Some(path)) => match path.into_os_string().into_string() {
                Ok(path) => {
                    self.python_config_path = path;
                    self.python_import_error = None;
                }
                Err(_) => self.python_import_error = Some("文件路径不是有效的 Unicode".into()),
            },
            Ok(None) => {}
            Err(e) => self.python_import_error = Some(format!("无法选择配置文件：{e}")),
        }
    }

    #[cfg(target_os = "linux")]
    fn open_picker(&mut self, ctx: &egui::Context, purpose: PickerPurpose) {
        let (send, receive) = std::sync::mpsc::channel();
        let ctx = ctx.clone();
        std::thread::spawn(move || {
            let result = match purpose {
                PickerPurpose::PythonConfig => crate::linux_ui::pick_python_config(),
                PickerPurpose::RemoteFolder => crate::linux_ui::pick_remote_folder(),
            };
            let _ = send.send(result);
            ctx.request_repaint();
        });
        self.picker = Some((purpose, receive));
    }

    #[cfg(target_os = "linux")]
    fn update_picker(&mut self) {
        use std::sync::mpsc::TryRecvError;
        let Some((purpose, receiver)) = &self.picker else {
            return;
        };
        let result = match receiver.try_recv() {
            Ok(result) => result,
            Err(TryRecvError::Empty) => return,
            Err(TryRecvError::Disconnected) => Err("桌面文件选择器提前退出".into()),
        };
        let purpose = *purpose;
        self.picker = None;
        match purpose {
            PickerPurpose::PythonConfig => self.apply_python_picker_result(result),
            PickerPurpose::RemoteFolder => self.apply_remote_picker_result(result),
        }
    }

    fn import_python_settings(&mut self) {
        self.python_import_error = None;
        let result =
            settings::import_python(Path::new(self.python_config_path.trim()), &self.settings)
                .and_then(|next| {
                    settings::save(&next).map_err(|e| format!("保存导入后的配置失败：{e}"))?;
                    Ok(next)
                });
        match result {
            Ok(next) => {
                self.reset_worktree_view();
                self.settings = next;
                self.reset_filter_editor();
                self.refresh();
                self.notice = "已导入 Python 配置。请确认工作目录后开启监听".into();
            }
            Err(e) => self.python_import_error = Some(format!("导入 Python 配置失败：{e}")),
        }
    }

    fn reset_worktree_view(&mut self) {
        self.remote_init = None;
        self.monitor = None;
        self.settings.auto_check = false;
        self.clear_changes();
        self.history.clear();
        self.history_selected = None;
        self.restore_confirm = false;
        self.backup_tag.clear();
        self.message_edited = false;
        self.prefill_message();
    }

    fn clear_changes(&mut self) {
        self.changes.clear();
        self.reviewed = None;
        self.selected_path.clear();
        self.diff.clear();
        self.pending_backup = false;
    }
}
