use crate::filters::FilterMatcher;
use eframe::egui;
use notify::{Event, EventKind, RecommendedWatcher, RecursiveMode, Watcher};
use std::{
    collections::HashSet,
    path::{Path, PathBuf},
    sync::{
        atomic::{AtomicBool, Ordering},
        mpsc::{self, Receiver, TrySendError},
        Arc,
    },
    time::{Duration, Instant},
};

pub struct Monitor {
    _watcher: RecommendedWatcher,
    events: Receiver<notify::Result<Event>>,
    overflow: Arc<AtomicBool>,
    root: PathBuf,
    filters: FilterMatcher,
    pending: HashSet<PathBuf>,
    last_event: Option<Instant>,
}

impl Monitor {
    pub fn start(root: &Path, filters: FilterMatcher, ctx: &egui::Context) -> Result<Self, String> {
        let (sender, events) = mpsc::sync_channel(4096);
        let overflow = Arc::new(AtomicBool::new(false));
        let sender_overflow = overflow.clone();
        let ctx = ctx.clone();
        let mut watcher = notify::recommended_watcher(move |event| {
            match sender.try_send(event) {
                Ok(()) => {}
                Err(TrySendError::Full(_)) => sender_overflow.store(true, Ordering::Release),
                Err(TrySendError::Disconnected(_)) => return,
            }
            ctx.request_repaint();
        })
        .map_err(|e| e.to_string())?;
        watcher
            .watch(root, RecursiveMode::Recursive)
            .map_err(|e| e.to_string())?;
        Ok(Self {
            _watcher: watcher,
            events,
            overflow,
            root: root.to_owned(),
            filters,
            pending: HashSet::new(),
            last_event: None,
        })
    }

    pub fn set_filters(&mut self, filters: FilterMatcher) {
        self.filters = filters;
    }

    pub fn collect(
        &mut self,
        debounce: Duration,
        ctx: &egui::Context,
    ) -> Result<Option<HashSet<PathBuf>>, String> {
        if self.overflow.load(Ordering::Acquire) {
            return Err("文件事件积压过多，请手动扫描后重新开启监听".into());
        }
        let now = Instant::now();
        // Bound the number of events handled per frame.
        for event in self.events.try_iter().take(4096) {
            let event = event.map_err(|e| e.to_string())?;
            if event.need_rescan() {
                return Err("系统文件事件队列溢出，请手动扫描后重新开启监听".into());
            }
            let paths = relevant_paths(&self.root, event, &self.filters);
            if !paths.is_empty() {
                self.pending.extend(paths);
                self.last_event = Some(now);
            }
        }
        if let Some(last) = self.last_event {
            let remaining = debounce.saturating_sub(last.elapsed());
            if remaining.is_zero() {
                self.last_event = None;
                return Ok(Some(std::mem::take(&mut self.pending)));
            }
            ctx.request_repaint_after(remaining);
        }
        // Also drain events that were queued while processing this frame.
        ctx.request_repaint_after(Duration::from_millis(250));
        Ok(None)
    }
}

fn relevant_paths(root: &Path, event: Event, filters: &FilterMatcher) -> Vec<PathBuf> {
    if !matches!(
        event.kind,
        EventKind::Any | EventKind::Create(_) | EventKind::Modify(_) | EventKind::Remove(_)
    ) {
        return Vec::new();
    }
    event
        .paths
        .into_iter()
        .filter_map(|path| {
            let relative = path.strip_prefix(root).ok()?;
            let name = relative.to_str()?;
            (!name.is_empty() && !filters.is_excluded(name)).then(|| relative.to_owned())
        })
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::filters::FilterSettings;
    use notify::event::{AccessKind, ModifyKind, RenameMode};

    #[test]
    fn keeps_both_rename_paths_and_excludes_noise() {
        let root = tempfile::tempdir().unwrap();
        let root = root.path();
        let filters = FilterSettings::default().compile().unwrap();
        let event = Event::new(EventKind::Modify(ModifyKind::Name(RenameMode::Both)))
            .add_path(root.join("before.docx"))
            .add_path(root.join("after.docx"))
            .add_path(root.join("~$after.docx"))
            .add_path(root.join(".git/index"))
            .add_path(root.parent().unwrap().join("outside.docx"));
        assert_eq!(
            relevant_paths(root, event, &filters),
            [PathBuf::from("before.docx"), PathBuf::from("after.docx")]
        );
        let event =
            Event::new(EventKind::Access(AccessKind::Read)).add_path(root.join("before.docx"));
        assert!(relevant_paths(root, event, &filters).is_empty());
    }

    #[test]
    fn recursive_watcher_delivers_file_event() {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path().canonicalize().unwrap();
        std::fs::create_dir(root.join("nested")).unwrap();
        let ctx = egui::Context::default();
        let filters = FilterSettings::default().compile().unwrap();
        let mut monitor = Monitor::start(&root, filters, &ctx).unwrap();
        std::fs::write(root.join("nested").join("文档.txt"), "测试").unwrap();
        let target = Path::new("nested").join("文档.txt");
        let deadline = Instant::now() + Duration::from_secs(5);
        while Instant::now() < deadline {
            if monitor
                .collect(Duration::from_millis(30), &ctx)
                .unwrap()
                .is_some_and(|paths| paths.contains(&target))
            {
                return;
            }
            std::thread::sleep(Duration::from_millis(15));
        }
        panic!("未收到递归文件监听事件");
    }
}
