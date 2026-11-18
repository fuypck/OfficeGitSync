use raw_window_handle::{HasWindowHandle, RawWindowHandle};
use std::{ffi::OsString, os::windows::ffi::OsStringExt, path::PathBuf};
use windows::{
    core::{w, HRESULT, PCWSTR},
    Win32::{
        Foundation::{HINSTANCE, HWND, LPARAM, WPARAM},
        System::Com::{
            CoCreateInstance, CoInitializeEx, CoTaskMemFree, CoUninitialize, CLSCTX_INPROC_SERVER,
            COINIT_APARTMENTTHREADED,
        },
        System::LibraryLoader::GetModuleHandleW,
        UI::{
            Shell::{
                Common::COMDLG_FILTERSPEC, FileOpenDialog, IFileOpenDialog, FOS_FILEMUSTEXIST,
                FOS_FORCEFILESYSTEM, FOS_PATHMUSTEXIST, FOS_PICKFOLDERS, SIGDN_FILESYSPATH,
            },
            WindowsAndMessaging::{
                IsIconic, IsWindowVisible, KillTimer, LoadIconW, MessageBoxIndirectW, PostMessageW,
                SendMessageW, SetTimer, ICON_BIG, MB_USERICON, MSGBOXPARAMSW, WM_PAINT, WM_SETICON,
            },
        },
    },
};

pub fn hwnd(window: &impl HasWindowHandle) -> Result<HWND, String> {
    match window.window_handle().map_err(|e| e.to_string())?.as_raw() {
        RawWindowHandle::Win32(handle) => Ok(HWND(handle.hwnd.get() as _)),
        _ => Err("无法取得 Windows 主窗口句柄".into()),
    }
}

const APP_ICON: PCWSTR = PCWSTR(std::ptr::without_provenance(1));

pub fn set_taskbar_icon(hwnd: HWND) -> windows::core::Result<()> {
    // Resource icons loaded through LoadIconW are shared and must not be destroyed.
    unsafe {
        let module = GetModuleHandleW(PCWSTR::null())?;
        let icon = LoadIconW(HINSTANCE(module.0), APP_ICON)?;
        SendMessageW(
            hwnd,
            WM_SETICON,
            WPARAM(ICON_BIG as usize),
            LPARAM(icon.0 as isize),
        );
    }
    Ok(())
}

pub fn show_startup_error(error: &str) {
    let message: Vec<_> = format!("OfficeGitSync 启动失败\n{error}")
        .encode_utf16()
        .chain(Some(0))
        .collect();
    // The message and the executable's icon resource stay valid until the dialog closes.
    unsafe {
        if let Ok(module) = GetModuleHandleW(PCWSTR::null()) {
            let params = MSGBOXPARAMSW {
                cbSize: std::mem::size_of::<MSGBOXPARAMSW>() as u32,
                hInstance: HINSTANCE(module.0),
                lpszText: PCWSTR(message.as_ptr()),
                lpszCaption: w!("OfficeGitSync"),
                dwStyle: MB_USERICON,
                lpszIcon: APP_ICON,
                ..Default::default()
            };
            MessageBoxIndirectW(&params);
        }
    }
}

pub struct HiddenWindowPump {
    hwnd: HWND,
    timer: usize,
}

impl HiddenWindowPump {
    pub fn start(hwnd: HWND) -> windows::core::Result<Self> {
        // The timer belongs to the GUI thread and continues running when its window is hidden.
        let timer = unsafe { SetTimer(hwnd, 0x4f47, 250, Some(paint_hidden_window)) };
        if timer == 0 {
            return Err(windows::core::Error::from_win32());
        }
        Ok(Self { hwnd, timer })
    }
}

impl Drop for HiddenWindowPump {
    fn drop(&mut self) {
        // The HWND is owned by eframe. A destroyed window has already lost its timers.
        let _ = unsafe { KillTimer(self.hwnd, self.timer) };
    }
}

unsafe extern "system" fn paint_hidden_window(hwnd: HWND, _: u32, _: usize, _: u32) {
    // Winit handles an explicit WM_PAINT, whereas its normal redraw request depends on visibility.
    unsafe {
        if !IsWindowVisible(hwnd).as_bool() || IsIconic(hwnd).as_bool() {
            let _ = PostMessageW(hwnd, WM_PAINT, WPARAM(0), LPARAM(0));
        }
    }
}

pub fn pick_folder(owner: HWND, title: &str) -> windows::core::Result<Option<PathBuf>> {
    pick_path(owner, title, true)
}

pub fn pick_python_config(owner: HWND) -> windows::core::Result<Option<PathBuf>> {
    pick_path(owner, "选择旧版 Python 配置", false)
}

fn pick_path(owner: HWND, title: &str, folder: bool) -> windows::core::Result<Option<PathBuf>> {
    struct ComApartment;
    impl Drop for ComApartment {
        fn drop(&mut self) {
            // Balances this function's successful CoInitializeEx call, including S_FALSE.
            unsafe { CoUninitialize() };
        }
    }

    // The native dialog and its COM objects stay on the owning GUI thread.
    unsafe {
        CoInitializeEx(None, COINIT_APARTMENTTHREADED).ok()?;
        let _apartment = ComApartment;
        let dialog: IFileOpenDialog =
            CoCreateInstance(&FileOpenDialog, None, CLSCTX_INPROC_SERVER)?;
        let options = dialog.GetOptions()? | FOS_FORCEFILESYSTEM;
        if folder {
            dialog.SetOptions(options | FOS_PICKFOLDERS)?;
        } else {
            dialog.SetOptions(options | FOS_FILEMUSTEXIST | FOS_PATHMUSTEXIST)?;
            dialog.SetFileTypes(&[COMDLG_FILTERSPEC {
                pszName: w!("旧版 Python 配置 (*.json)"),
                pszSpec: w!("*.json"),
            }])?;
        }
        let title: Vec<_> = title.encode_utf16().chain(Some(0)).collect();
        dialog.SetTitle(PCWSTR(title.as_ptr()))?;
        match dialog.Show(owner) {
            Ok(()) => {}
            Err(e) if e.code() == HRESULT::from_win32(1223) => return Ok(None),
            Err(e) => return Err(e),
        }
        let item = dialog.GetResult()?;
        let name = item.GetDisplayName(SIGDN_FILESYSPATH)?;
        let path = PathBuf::from(OsString::from_wide(name.as_wide()));
        CoTaskMemFree(Some(name.0.cast()));
        Ok(Some(path))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::time::{Duration, Instant};
    use windows::Win32::UI::WindowsAndMessaging::{
        CreateWindowExW, DestroyWindow, DispatchMessageW, PeekMessageW, MSG, PM_REMOVE,
        WINDOW_EX_STYLE, WINDOW_STYLE,
    };

    #[test]
    fn hidden_window_gets_a_paint_message_from_the_native_timer() {
        // Create a real hidden window on this test thread, then dispatch its timer messages.
        unsafe {
            let hwnd = CreateWindowExW(
                WINDOW_EX_STYLE(0),
                w!("STATIC"),
                w!("OfficeGitSync test"),
                WINDOW_STYLE(0),
                0,
                0,
                50,
                50,
                HWND::default(),
                None,
                HINSTANCE::default(),
                None,
            )
            .unwrap();
            let pump = HiddenWindowPump::start(hwnd).unwrap();
            let mut painted = false;
            let deadline = Instant::now() + Duration::from_secs(3);
            while Instant::now() < deadline && !painted {
                let mut message = MSG::default();
                while PeekMessageW(&mut message, hwnd, 0, 0, PM_REMOVE).as_bool() {
                    painted |= message.message == WM_PAINT;
                    DispatchMessageW(&message);
                }
                std::thread::sleep(Duration::from_millis(10));
            }
            drop(pump);
            DestroyWindow(hwnd).unwrap();
            assert!(painted);
        }
    }
}
