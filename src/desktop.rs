use eframe::egui;
use std::sync::mpsc::{self, Receiver, Sender};

#[derive(Clone, Copy)]
pub enum Action {
    Show,
    Quit,
    #[cfg(target_os = "linux")]
    TrayUnavailable,
}

#[derive(Clone)]
struct Dispatcher {
    sender: Sender<Action>,
    ctx: egui::Context,
}

impl Dispatcher {
    fn send(&self, action: Action) {
        if self.sender.send(action).is_ok() {
            self.ctx.request_repaint();
        }
    }
}

pub struct Desktop {
    events: Receiver<Action>,
    #[cfg(target_os = "linux")]
    tray: ksni::blocking::Handle<Dispatcher>,
    #[cfg(target_os = "windows")]
    _tray: tray_icon::TrayIcon,
    #[cfg(target_os = "windows")]
    _pump: crate::windows_ui::HiddenWindowPump,
}

impl Desktop {
    pub fn start(cc: &eframe::CreationContext<'_>) -> Result<Self, String> {
        let (sender, events) = mpsc::channel();
        let dispatcher = Dispatcher {
            sender,
            ctx: cc.egui_ctx.clone(),
        };
        #[cfg(target_os = "linux")]
        {
            use ksni::blocking::TrayMethods;
            let tray = dispatcher.spawn().map_err(|e| e.to_string())?;
            Ok(Self { events, tray })
        }
        #[cfg(target_os = "windows")]
        {
            use tray_icon::menu::{Menu, MenuEvent, MenuItem};
            let hwnd = crate::windows_ui::hwnd(cc)?;
            let pump = crate::windows_ui::HiddenWindowPump::start(hwnd)
                .map_err(|e| format!("无法启动后台窗口消息处理：{e}"))?;
            let menu = Menu::new();
            menu.append(&MenuItem::with_id("show", "显示主窗口", true, None))
                .and_then(|_| menu.append(&MenuItem::with_id("quit", "退出程序", true, None)))
                .map_err(|e| e.to_string())?;
            let artwork = crate::icon::load();
            let icon = tray_icon::Icon::from_rgba(artwork.rgba, artwork.width, artwork.height)
                .map_err(|e| e.to_string())?;
            let tray = tray_icon::TrayIconBuilder::new()
                .with_icon(icon)
                .with_tooltip("OfficeGitSync")
                .with_menu(Box::new(menu))
                .with_menu_on_left_click(false)
                .build()
                .map_err(|e| e.to_string())?;
            let icon_dispatcher = dispatcher.clone();
            tray_icon::TrayIconEvent::set_event_handler(Some(move |event| {
                if matches!(
                    event,
                    tray_icon::TrayIconEvent::Click {
                        button: tray_icon::MouseButton::Left,
                        button_state: tray_icon::MouseButtonState::Up,
                        ..
                    }
                ) {
                    icon_dispatcher.send(Action::Show);
                }
            }));
            MenuEvent::set_event_handler(Some(move |event: MenuEvent| match event.id.0.as_str() {
                "show" => dispatcher.send(Action::Show),
                "quit" => dispatcher.send(Action::Quit),
                _ => {}
            }));
            Ok(Self {
                events,
                _tray: tray,
                _pump: pump,
            })
        }
        #[cfg(not(any(target_os = "linux", target_os = "windows")))]
        {
            let _ = (dispatcher, events);
            Err("当前平台不支持系统托盘".into())
        }
    }

    pub fn actions(&self) -> impl Iterator<Item = Action> + '_ {
        self.events.try_iter()
    }

    pub fn available(&self) -> bool {
        #[cfg(target_os = "linux")]
        return !self.tray.is_closed();
        #[cfg(not(target_os = "linux"))]
        true
    }
}

#[cfg(target_os = "linux")]
impl ksni::Tray for Dispatcher {
    fn id(&self) -> String {
        "officegitsync".into()
    }

    fn title(&self) -> String {
        "OfficeGitSync 文档备份".into()
    }

    fn icon_pixmap(&self) -> Vec<ksni::Icon> {
        let artwork = crate::icon::load();
        let mut data = artwork.rgba;
        for pixel in data.as_chunks_mut::<4>().0 {
            pixel.rotate_right(1);
        }
        vec![ksni::Icon {
            width: artwork.width as i32,
            height: artwork.height as i32,
            data,
        }]
    }

    fn activate(&mut self, _: i32, _: i32) {
        self.send(Action::Show);
    }

    fn menu(&self) -> Vec<ksni::MenuItem<Self>> {
        use ksni::menu::StandardItem;
        [("显示主窗口", Action::Show), ("退出程序", Action::Quit)]
            .into_iter()
            .map(|(label, action)| {
                StandardItem {
                    label: label.into(),
                    activate: Box::new(move |tray: &mut Self| tray.send(action)),
                    ..Default::default()
                }
                .into()
            })
            .collect()
    }

    fn watcher_offline(&self, _: ksni::OfflineReason) -> bool {
        self.send(Action::TrayUnavailable);
        false
    }
}
