#![cfg_attr(target_os = "windows", windows_subsystem = "windows")]

mod app;
mod autostart;
mod backup;
mod desktop;
mod filters;
mod fonts;
mod history;
mod icon;
mod instance;
#[cfg(target_os = "linux")]
mod linux_ui;
mod monitor;
mod remote;
mod settings;
#[cfg(target_os = "windows")]
mod windows_ui;

use eframe::egui;

fn main() -> eframe::Result<()> {
    let result = run();
    #[cfg(target_os = "windows")]
    if let Err(error) = &result {
        windows_ui::show_startup_error(&error.to_string());
    }
    result
}

fn run() -> eframe::Result<()> {
    let instance = settings::config_dir()
        .and_then(|dir| instance::Instance::acquire(&dir).map_err(|e| e.to_string()))
        .map_err(|e| eframe::Error::AppCreation(std::io::Error::other(e).into()))?;
    let Some(instance) = instance else {
        return Ok(());
    };
    let options = eframe::NativeOptions {
        viewport: egui::ViewportBuilder::default()
            .with_inner_size([850.0, 720.0])
            .with_min_inner_size([640.0, 520.0])
            .with_icon(icon::load()),
        ..Default::default()
    };
    eframe::run_native(
        "OfficeGitSync",
        options,
        Box::new(move |cc| {
            #[cfg(target_os = "windows")]
            windows_ui::hwnd(cc)
                .and_then(|hwnd| windows_ui::set_taskbar_icon(hwnd).map_err(|e| e.to_string()))
                .map_err(std::io::Error::other)?;
            fonts::setup(&cc.egui_ctx);
            Ok(Box::new(app::App::new(cc, instance)))
        }),
    )
}
