use crate::filters::FilterSettings;
use git2::{
    AttrCheckFlags, AttrValue, DiffOptions, ErrorCode, IndexEntryExtendedFlag, Oid, Repository,
    RepositoryState, Signature, Status, StatusOptions,
};
use std::{
    collections::HashSet,
    fs::{self, File, OpenOptions},
    io,
    path::{Component, Path, PathBuf},
};

pub struct Change {
    pub path: String,
    pub status: String,
    pub selected: bool,
}

fn status(repo: &Repository, filters: &FilterSettings) -> Result<Vec<Change>, String> {
    let matcher = filters.compile()?;
    let mut opts = StatusOptions::new();
    opts.include_untracked(true).recurse_untracked_dirs(true);
    let statuses = repo.statuses(Some(&mut opts)).map_err(|e| e.to_string())?;
    Ok(statuses
        .iter()
        .filter_map(|entry| {
            let path = entry.path()?;
            (!matcher.is_excluded(path)).then(|| Change {
                path: path.to_owned(),
                status: status_label(entry.status()),
                selected: false,
            })
        })
        .collect())
}

fn status_label(status: Status) -> String {
    let labels = [
        (Status::CONFLICTED, "冲突"),
        (Status::WT_NEW | Status::INDEX_NEW, "新增"),
        (Status::WT_MODIFIED | Status::INDEX_MODIFIED, "修改"),
        (Status::WT_DELETED | Status::INDEX_DELETED, "删除"),
        (Status::WT_RENAMED | Status::INDEX_RENAMED, "重命名"),
        (Status::WT_TYPECHANGE | Status::INDEX_TYPECHANGE, "类型变更"),
    ];
    labels
        .into_iter()
        .filter_map(|(flag, label)| status.intersects(flag).then_some(label))
        .collect::<Vec<_>>()
        .join("、")
}

#[derive(PartialEq, Eq)]
pub struct Revision {
    reference: String,
    oid: Option<Oid>,
}

pub fn revision(repo: &Repository) -> Result<Revision, String> {
    let head = repo.find_reference("HEAD").map_err(|e| e.to_string())?;
    if let Some(reference) = head.symbolic_target() {
        let oid = match repo.find_reference(reference) {
            Ok(target) => Some(target.peel_to_commit().map_err(|e| e.to_string())?.id()),
            Err(e) if e.code() == ErrorCode::NotFound => None,
            Err(e) => return Err(e.to_string()),
        };
        Ok(Revision {
            reference: reference.to_owned(),
            oid,
        })
    } else {
        Ok(Revision {
            reference: "HEAD".into(),
            oid: head.target(),
        })
    }
}

pub fn scan(
    repo: &Repository,
    filters: &FilterSettings,
) -> Result<(Revision, Vec<Change>), String> {
    let before = revision(repo)?;
    let changes = status(repo, filters)?;
    if before != revision(repo)? {
        return Err("扫描期间 Git 分支发生变化，请重新扫描".into());
    }
    Ok((before, changes))
}

struct IndexLock {
    path: PathBuf,
    file: Option<File>,
    published: bool,
}

impl IndexLock {
    fn acquire(index: &Path) -> io::Result<Self> {
        let path = index.with_extension("lock");
        let file = OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&path)?;
        Ok(Self {
            path,
            file: Some(file),
            published: false,
        })
    }
}

impl Drop for IndexLock {
    fn drop(&mut self) {
        self.file.take();
        if !self.published {
            let _ = fs::remove_file(&self.path);
        }
    }
}

fn stage_path(
    repo: &Repository,
    index: &mut git2::Index,
    workdir: &Path,
    relative: &Path,
) -> Result<(), String> {
    if relative.as_os_str().is_empty()
        || !relative
            .components()
            .all(|part| matches!(part, Component::Normal(_)))
    {
        return Err(format!("不安全的 Git 路径：{}", relative.display()));
    }
    // A directory replaced by a link must not redirect an approved path outside the worktree.
    for parent in relative
        .ancestors()
        .skip(1)
        .filter(|path| !path.as_os_str().is_empty())
    {
        if let Ok(metadata) = fs::symlink_metadata(workdir.join(parent)) {
            #[cfg(windows)]
            let is_link = {
                use std::os::windows::fs::MetadataExt;
                metadata.file_attributes() & 0x400 != 0 // FILE_ATTRIBUTE_REPARSE_POINT
            };
            #[cfg(not(windows))]
            let is_link = metadata.file_type().is_symlink();
            if is_link {
                return Err(format!("文件位于链接目录中：{}", relative.display()));
            }
        }
    }
    let result = match fs::symlink_metadata(workdir.join(relative)) {
        Ok(_) => {
            for attribute in ["filter", "working-tree-encoding"] {
                let value = repo
                    .get_attr_bytes(relative, attribute, AttrCheckFlags::FILE_THEN_INDEX)
                    .map_err(|e| e.to_string())?;
                if matches!(AttrValue::always_bytes(value), AttrValue::Bytes(_)) {
                    return Err(format!("{} 需要 Git 属性 {attribute} 指定的内容转换，请使用支持该转换的 Git 客户端处理", relative.display()));
                }
            }
            index.add_path(relative)
        }
        Err(error)
            if matches!(
                error.kind(),
                io::ErrorKind::NotFound | io::ErrorKind::NotADirectory
            ) =>
        {
            index.remove_path(relative)
        }
        Err(error) => return Err(format!("无法读取 {}：{error}", relative.display())),
    };
    result.map_err(|error| format!("无法暂存 {}：{error}", relative.display()))
}

pub fn commit(
    repo: &Repository,
    reviewed: &Revision,
    filters: &FilterSettings,
    selected: &HashSet<String>,
    message: &str,
) -> Result<Oid, String> {
    if selected.is_empty() {
        return Err("没有选择要提交的文件".into());
    }
    if message.trim().is_empty() {
        return Err("请输入提交说明".into());
    }
    let index = repo.index().map_err(|e| e.to_string())?;
    let index_path = index.path().ok_or("无法定位 Git 暂存区")?;
    let mut lock = IndexLock::acquire(index_path)
        .map_err(|e| format!("无法锁定 Git 暂存区，请检查是否有其他 Git 操作：{e}"))?;
    commit_locked(
        repo, reviewed, filters, selected, message, index_path, &mut lock,
    )
}

fn commit_locked(
    repo: &Repository,
    reviewed: &Revision,
    filters: &FilterSettings,
    selected: &HashSet<String>,
    message: &str,
    index_path: &Path,
    lock: &mut IndexLock,
) -> Result<Oid, String> {
    let workdir = repo.workdir().ok_or("裸仓库没有可备份的工作区")?;
    if repo.state() != RepositoryState::Clean {
        return Err("请先在 Git 客户端结束正在进行的合并、变基或其他操作".into());
    }
    let mut transaction = repo.transaction().map_err(|e| e.to_string())?;
    transaction.lock_ref("HEAD").map_err(|e| e.to_string())?;
    let head = repo.find_reference("HEAD").map_err(|e| e.to_string())?;
    let branch = head
        .symbolic_target()
        .ok_or("请先切换到本地分支再创建备份")?;
    transaction.lock_ref(branch).map_err(|e| e.to_string())?;
    let parent = match repo.find_reference(branch) {
        Ok(reference) => Some(reference.peel_to_commit().map_err(|e| e.to_string())?),
        Err(e) if e.code() == ErrorCode::NotFound => None,
        Err(e) => return Err(e.to_string()),
    };
    if branch != reviewed.reference || parent.as_ref().map(git2::Commit::id) != reviewed.oid {
        return Err("Git 分支或提交已变化，请重新扫描并确认备份".into());
    }
    let parent_tree = parent
        .as_ref()
        .map(git2::Commit::tree)
        .transpose()
        .map_err(|e| e.to_string())?;
    let mut index = repo.index().map_err(|e| e.to_string())?;
    index.read(true).map_err(|e| e.to_string())?;
    let staged = repo
        .diff_tree_to_index(parent_tree.as_ref(), Some(&index), None)
        .map_err(|e| e.to_string())?;
    if index.has_conflicts()
        || staged.deltas().len() != 0
        || index.iter().any(|entry| {
            IndexEntryExtendedFlag::from_bits_truncate(entry.flags_extended).is_intent_to_add()
        })
    {
        return Err("暂存区已有变更。请先在 Git 客户端处理，避免丢失部分暂存内容".into());
    }
    let current = status(repo, filters)?;
    if !selected
        .iter()
        .all(|path| current.iter().any(|change| change.path == *path))
    {
        return Err("选中文件的状态已变化，请刷新并重新确认".into());
    }

    let scratch = tempfile::tempdir_in(repo.path()).map_err(|e| e.to_string())?;
    let scratch_index = scratch.path().join("index");
    if index_path.exists() {
        fs::copy(index_path, &scratch_index).map_err(|e| e.to_string())?;
    }
    // Bind the scratch index to a separate repository handle. libgit2 can apply repository
    // attributes to added paths without updating the real index or its in-memory cache.
    let staging_repo = Repository::open(workdir).map_err(|e| e.to_string())?;
    if staging_repo.path() != repo.path() {
        return Err("工作区对应的 Git 仓库已改变，请重新扫描".into());
    }
    let mut prepared = git2::Index::open(&scratch_index).map_err(|e| e.to_string())?;
    staging_repo
        .set_index(&mut prepared)
        .map_err(|e| e.to_string())?;
    for path in selected {
        stage_path(&staging_repo, &mut prepared, workdir, Path::new(path))?;
    }
    prepared.write().map_err(|e| e.to_string())?;

    let tree_id = prepared.write_tree_to(repo).map_err(|e| e.to_string())?;
    let tree = repo.find_tree(tree_id).map_err(|e| e.to_string())?;
    let mut opts = DiffOptions::new();
    opts.ignore_filemode(false);
    let diff = repo
        .diff_tree_to_tree(parent_tree.as_ref(), Some(&tree), Some(&mut opts))
        .map_err(|e| e.to_string())?;
    if diff.deltas().len() == 0 {
        return Err("选中文件没有可提交的变更".into());
    }
    for delta in diff.deltas() {
        for file in [delta.old_file(), delta.new_file()] {
            if !file
                .path_bytes()
                .and_then(|bytes| std::str::from_utf8(bytes).ok())
                .is_some_and(|path| selected.contains(path))
            {
                return Err("实际变更包含未确认的路径，请刷新后重试".into());
            }
        }
    }

    let signature = repo
        .signature()
        .or_else(|_| Signature::now("OfficeGitSync", "officegitsync@localhost"))
        .map_err(|e| e.to_string())?;
    let parents: Vec<_> = parent.iter().collect();
    let oid = repo
        .commit(None, &signature, &signature, message, &tree, &parents)
        .map_err(|e| e.to_string())?;
    let file = lock.file.as_mut().expect("held index lock");
    io::copy(
        &mut File::open(&scratch_index).map_err(|e| e.to_string())?,
        file,
    )
    .and_then(|_| file.sync_all())
    .map_err(|e| e.to_string())?;
    lock.file.take();
    transaction
        .set_target(branch, oid, Some(&signature), "OfficeGitSync: backup")
        .map_err(|e| e.to_string())?;
    transaction.commit().map_err(|e| e.to_string())?;
    fs::rename(&lock.path, index_path).map_err(|e| {
        format!("备份提交 {oid} 已创建，但暂存区更新失败：{e}。请在 Git 客户端检查暂存区")
    })?;
    lock.published = true;
    let _ = index.read(true);
    Ok(oid)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn commits_only_approved_paths_and_preserves_staged_changes() {
        let dir = tempfile::tempdir().unwrap();
        let repo = Repository::init(dir.path()).unwrap();
        let filters = FilterSettings::default();
        let approved = HashSet::from(["文档.txt".to_owned()]);
        let document = dir.path().join("文档.txt");
        fs::write(&document, "初版").unwrap();
        fs::write(dir.path().join("unrelated.txt"), "其他").unwrap();

        let first = commit(
            &repo,
            &revision(&repo).unwrap(),
            &filters,
            &approved,
            "初次备份",
        )
        .unwrap();
        let tree = repo.find_commit(first).unwrap().tree().unwrap();
        assert!(tree.get_path(Path::new("文档.txt")).is_ok());
        assert!(tree.get_path(Path::new("unrelated.txt")).is_err());

        let mut index = repo.index().unwrap();
        index.add_path(Path::new("unrelated.txt")).unwrap();
        index.write().unwrap();
        let before = fs::read(index.path().unwrap()).unwrap();
        fs::remove_file(document).unwrap();
        assert!(commit(
            &repo,
            &revision(&repo).unwrap(),
            &filters,
            &approved,
            "删除"
        )
        .is_err());
        assert_eq!(repo.head().unwrap().target(), Some(first));
        assert_eq!(fs::read(index.path().unwrap()).unwrap(), before);

        index.remove_path(Path::new("unrelated.txt")).unwrap();
        index.write().unwrap();
        let second = commit(
            &repo,
            &revision(&repo).unwrap(),
            &filters,
            &approved,
            "备份删除",
        )
        .unwrap();
        let second = repo.find_commit(second).unwrap();
        assert_eq!(second.parent_id(0).unwrap(), first);
        assert!(second.tree().unwrap().is_empty());
    }

    #[test]
    fn respects_existing_index_lock() {
        let dir = tempfile::tempdir().unwrap();
        let repo = Repository::init(dir.path()).unwrap();
        let lock = repo.path().join("index.lock");
        fs::write(&lock, b"external").unwrap();
        let approved = HashSet::from(["doc.txt".to_owned()]);
        assert!(commit(
            &repo,
            &revision(&repo).unwrap(),
            &FilterSettings::default(),
            &approved,
            "备份"
        )
        .is_err());
        assert_eq!(fs::read(lock).unwrap(), b"external");
    }

    #[test]
    fn rejects_confirmation_after_branch_switch() {
        let dir = tempfile::tempdir().unwrap();
        let repo = Repository::init(dir.path()).unwrap();
        let path = dir.path().join("文档.txt");
        let filters = FilterSettings::default();
        let selected = HashSet::from(["文档.txt".to_owned()]);
        fs::write(&path, "原始").unwrap();
        let initial = commit(
            &repo,
            &revision(&repo).unwrap(),
            &filters,
            &selected,
            "原始",
        )
        .unwrap();
        repo.branch("other", &repo.find_commit(initial).unwrap(), false)
            .unwrap();
        fs::write(&path, "更新").unwrap();
        let (reviewed, _) = scan(&repo, &filters).unwrap();
        repo.set_head("refs/heads/other").unwrap();
        let error = commit(&repo, &reviewed, &filters, &selected, "备份").unwrap_err();
        assert!(error.contains("分支或提交已变化"));
        assert_eq!(repo.head().unwrap().target(), Some(initial));
        assert_eq!(fs::read_to_string(path).unwrap(), "更新");
    }

    #[test]
    fn literal_paths_and_intent_to_add_are_handled_safely() {
        let dir = tempfile::tempdir().unwrap();
        let repo = Repository::init(dir.path()).unwrap();
        let filters = FilterSettings::default();
        let selected = HashSet::from(["a[1].txt".to_owned()]);
        fs::write(dir.path().join("a[1].txt"), "选中").unwrap();
        fs::write(dir.path().join("a1.txt"), "与通配符匹配").unwrap();
        let initial = revision(&repo).unwrap();
        let id = commit(&repo, &initial, &filters, &selected, "备份").unwrap();
        let tree = repo.find_commit(id).unwrap().tree().unwrap();
        assert!(tree.get_path(Path::new("a[1].txt")).is_ok());
        assert!(tree.get_path(Path::new("a1.txt")).is_err());

        let mut index = repo.index().unwrap();
        let mut entry = index.get_path(Path::new("a[1].txt"), 0).unwrap();
        entry.flags |= git2::IndexEntryFlag::EXTENDED.bits();
        entry.flags_extended |= IndexEntryExtendedFlag::INTENT_TO_ADD.bits();
        index.add(&entry).unwrap();
        index.write().unwrap();
        let path = repo.path().join("index");
        let index_before = fs::read(&path).unwrap();
        fs::write(dir.path().join("a[1].txt"), "更新").unwrap();
        let source = revision(&repo).unwrap();
        let error = commit(&repo, &source, &filters, &selected, "更新").unwrap_err();
        assert!(error.contains("暂存区已有变更"));
        assert_eq!(index_before, fs::read(path).unwrap());
        assert_eq!(repo.head().unwrap().target(), Some(id));
    }

    #[test]
    fn updates_selected_file_with_git_attributes_and_preserves_the_edited_message() {
        let dir = tempfile::tempdir().unwrap();
        let repo = Repository::init(dir.path()).unwrap();
        let filters = FilterSettings::default();
        let selected = HashSet::from(["备份.txt".to_owned()]);
        let document = dir.path().join("备份.txt");
        fs::write(dir.path().join(".gitattributes"), "*.txt text\n").unwrap();
        fs::write(&document, "初稿\r\n").unwrap();
        let reviewed = revision(&repo).unwrap();
        assert!(commit(&repo, &reviewed, &filters, &selected, "  ").is_err());
        let first = commit(&repo, &reviewed, &filters, &selected, "2026-11-18 15:30:25").unwrap();
        fs::write(&document, "第二稿\r\n").unwrap();
        let (reviewed, _) = scan(&repo, &filters).unwrap();
        let message = "  用户填写的版本说明  ";
        let id = commit(&repo, &reviewed, &filters, &selected, message).unwrap();
        let commit = repo.find_commit(id).unwrap();
        assert_eq!(commit.message(), Some(message));
        assert_eq!(commit.parent_id(0).unwrap(), first);
        let tree = commit.tree().unwrap();
        assert_eq!(tree.len(), 1);
        let entry = tree.get_path(Path::new("备份.txt")).unwrap();
        assert_eq!(
            repo.find_blob(entry.id()).unwrap().content(),
            "第二稿\n".as_bytes()
        );
        assert_eq!(fs::read(&document).unwrap(), "第二稿\r\n".as_bytes());
        let index = repo.index().unwrap();
        assert_eq!(
            index.get_path(Path::new("备份.txt"), 0).unwrap().id,
            entry.id()
        );
    }

    #[test]
    fn rejects_paths_that_require_external_content_conversion() {
        let dir = tempfile::tempdir().unwrap();
        let repo = Repository::init(dir.path()).unwrap();
        let filters = FilterSettings::default();
        fs::write(dir.path().join(".gitattributes"), "*.bin filter=lfs\n").unwrap();
        fs::write(dir.path().join("data.bin"), b"raw bytes").unwrap();
        let selected = HashSet::from(["data.bin".to_owned()]);
        let reviewed = revision(&repo).unwrap();
        let error = commit(&repo, &reviewed, &filters, &selected, "备份").unwrap_err();
        assert!(error.contains("Git 属性 filter"));
        assert!(repo.head().is_err());
        assert!(repo.index().unwrap().is_empty());
        assert!(!repo.path().join("index.lock").exists());
    }
}
