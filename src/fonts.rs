use eframe::egui;
#[cfg(any(target_os = "linux", target_os = "windows"))]
use std::fs;

pub fn setup(ctx: &egui::Context) {
    let mut fonts = egui::FontDefinitions::default();
    if let Some(font) = chinese_font() {
        const CHINESE: &str = "chinese";
        fonts.font_data.insert(CHINESE.into(), font);
        let proportional = fonts
            .families
            .entry(egui::FontFamily::Proportional)
            .or_default();
        #[cfg(target_os = "windows")]
        proportional.insert(0, CHINESE.into());
        #[cfg(not(target_os = "windows"))]
        proportional.push(CHINESE.into());
        fonts
            .families
            .entry(egui::FontFamily::Monospace)
            .or_default()
            .push(CHINESE.into());
    }
    ctx.set_fonts(fonts);
    #[cfg(target_os = "windows")]
    ctx.all_styles_mut(|style| {
        // Leave widget spacing and the viewport scale to egui. Enlarge only small UI text.
        for (role, size) in [
            (egui::TextStyle::Small, 10.0),
            (egui::TextStyle::Body, 13.0),
            (egui::TextStyle::Button, 13.0),
        ] {
            if let Some(font) = style.text_styles.get_mut(&role) {
                font.size = size;
            }
        }
    });
}

#[cfg(target_os = "linux")]
fn chinese_font() -> Option<egui::FontData> {
    let font = linux_chinese_font();
    if font.is_none() {
        eprintln!(
            "未找到可用的简体中文字体。在 Debian/Kali 上可安装 fontconfig 和 fonts-noto-cjk。"
        );
    }
    font
}

#[cfg(target_os = "windows")]
fn chinese_font() -> Option<egui::FontData> {
    let windows = std::env::var_os("WINDIR")
        .map(std::path::PathBuf::from)
        .unwrap_or_else(|| std::path::PathBuf::from(r"C:\Windows"));
    let directory = windows.join("Fonts");
    // Prefer the regular Microsoft YaHei face, then sans-serif fallbacks.
    [
        "msyh.ttc",
        "msyh.ttf",
        "Deng.ttf",
        "simhei.ttf",
        "simsun.ttc",
    ]
    .into_iter()
    .filter_map(|name| fs::read(directory.join(name)).ok())
    .find_map(windows_font_data)
}

#[cfg(target_os = "windows")]
fn windows_font_data(data: Vec<u8>) -> Option<egui::FontData> {
    use ab_glyph::{Font, FontRef};
    let face = FontRef::try_from_slice_and_index(&data, 0).ok()?;
    if !"中文备份".chars().all(|ch| {
        let glyph = face.glyph_id(ch);
        glyph.0 != 0 && face.outline(glyph).is_some()
    }) {
        return None;
    }
    Some(egui::FontData::from_owned(data))
}

#[cfg(not(any(target_os = "linux", target_os = "windows")))]
fn chinese_font() -> Option<egui::FontData> {
    None
}

#[cfg(target_os = "linux")]
fn linux_chinese_font() -> Option<egui::FontData> {
    let output = std::process::Command::new("fc-match")
        .args([
            "--format",
            "%{file}\t%{index}\t%{lang}\n",
            "sans-serif:lang=zh-cn:charset=4e2d:outline=true",
        ])
        .output()
        .ok()?;
    if !output.status.success() {
        return None;
    }

    let mut fields = output.stdout.splitn(3, |&byte| byte == b'\t');
    let file = fields.next()?;
    let index = std::str::from_utf8(fields.next()?)
        .ok()?
        .parse::<u32>()
        .ok()?;
    let languages = std::str::from_utf8(fields.next()?).ok()?.trim_end();
    if !languages.split('|').any(|language| language == "zh-cn") {
        return None;
    }

    use std::os::unix::ffi::OsStrExt;
    let data = fs::read(std::ffi::OsStr::from_bytes(file)).ok()?;
    let mut font = egui::FontData::from_owned(data);
    // Fontconfig packs the variable-font instance into the upper 16 bits.
    font.index = index & 0xffff;
    Some(font)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn text_shapes_stay_on_physical_pixels_at_fractional_display_scales() {
        for scale in [1.0, 1.25, 1.5, 1.75, 2.0] {
            let ctx = egui::Context::default();
            #[cfg(target_os = "windows")]
            setup(&ctx);
            let mut input = egui::RawInput::default();
            input
                .viewports
                .get_mut(&egui::ViewportId::ROOT)
                .unwrap()
                .native_pixels_per_point = Some(scale);
            let output = ctx.run(input, |ctx| {
                let font = egui::TextStyle::Body.resolve(&ctx.style());
                let text = if ctx.fonts(|fonts| fonts.has_glyphs(&font, "中文备份")) {
                    "OfficeGitSync 中文备份"
                } else {
                    "OfficeGitSync"
                };
                ctx.layer_painter(egui::LayerId::background()).text(
                    egui::pos2(13.37, 17.29),
                    egui::Align2::LEFT_TOP,
                    text,
                    font,
                    egui::Color32::WHITE,
                );
            });
            assert!((output.pixels_per_point - scale).abs() < f32::EPSILON);
            let shapes = ctx.tessellate(output.shapes, output.pixels_per_point);
            assert!(!shapes.is_empty());
            for shape in shapes {
                if let egui::epaint::Primitive::Mesh(mesh) = shape.primitive {
                    assert!(!mesh.vertices.is_empty());
                    for vertex in mesh.vertices {
                        for point in [vertex.pos.x, vertex.pos.y] {
                            let pixel = point * scale;
                            assert!(
                                (pixel - pixel.round()).abs() < 0.001,
                                "scale {scale}: {pixel}"
                            );
                        }
                    }
                }
            }
        }
    }

    #[cfg(target_os = "windows")]
    #[test]
    fn rejects_invalid_windows_font_data_and_keeps_monospace_first() {
        assert!(windows_font_data(vec![1, 2, 3]).is_none());
        let default_fonts = egui::FontDefinitions::default();
        let latin_name = &default_fonts.families[&egui::FontFamily::Monospace][0];
        assert!(windows_font_data(default_fonts.font_data[latin_name].font.to_vec()).is_none());
        let ctx = egui::Context::default();
        setup(&ctx);
        let _ = ctx.run(Default::default(), |ctx| {
            let mono = egui::FontId::monospace(12.0);
            ctx.fonts(|fonts| {
                assert!(
                    (fonts.glyph_width(&mono, 'i') - fonts.glyph_width(&mono, 'W')).abs() < 0.001
                );
            });
            for theme in [egui::Theme::Light, egui::Theme::Dark] {
                assert!(
                    (ctx.style_of(theme).text_styles[&egui::TextStyle::Body].size - 13.0).abs()
                        < f32::EPSILON
                );
            }
        });
    }
}
