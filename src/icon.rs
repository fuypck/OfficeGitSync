use eframe::egui::IconData;
use std::io::Cursor;

pub fn load() -> IconData {
    let mut reader = png::Decoder::new(Cursor::new(include_bytes!("../assets/officegitsync.png")))
        .read_info()
        .expect("valid OfficeGitSync PNG");
    let mut rgba = vec![0; reader.output_buffer_size().expect("bounded icon size")];
    let frame = reader.next_frame(&mut rgba).expect("valid icon frame");
    assert_eq!(frame.color_type, png::ColorType::Rgba);
    assert_eq!(frame.bit_depth, png::BitDepth::Eight);
    rgba.truncate(frame.buffer_size());
    IconData {
        rgba,
        width: frame.width,
        height: frame.height,
    }
}

#[cfg(test)]
mod tests {
    #[test]
    fn window_icon_has_transparent_edges_and_visible_artwork() {
        let icon = super::load();
        assert_eq!((icon.width, icon.height), (128, 128));
        assert_eq!(icon.rgba[3], 0);
        assert_eq!(icon.rgba[((icon.width * 64 + 64) * 4 + 3) as usize], 255);
    }
}
