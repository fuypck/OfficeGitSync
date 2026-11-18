use chrono::{DateTime, FixedOffset};
use git2::{ErrorCode, Oid, Repository, Sort, Tree};
use std::{
    collections::HashMap,
    fs::{self, OpenOptions},
    io::Write,
    path::{Component, Path, PathBuf},
};

const LEGACY_TIMELINE: &str = "refs/heads/officegitsync-timeline";

pub struct Version {
    pub id: Oid,
    pub current: bool,
    pub message: String,
    pub date: String,
    pub tags: Vec<String>,
}

pub fn versions(repo: &Repository) -> Result<Vec<Version>, git2::Error> {
    let head = match repo.head() {
        Ok(head) => Some(head.peel_to_commit()?.id()),
        Err(e) if e.code() == ErrorCode::UnbornBranch => None,
        Err(e) => return Err(e),
    };
    let mut tags = HashMap::<Oid, Vec<String>>::new();
    for reference in repo.references_glob("refs/tags/*")? {
        let reference = reference?;
        if let (Some(name), Ok(commit)) = (reference.shorthand(), reference.peel_to_commit()) {
            tags.entry(commit.id()).or_default().push(name.to_owned());
        }
    }
    let mut walk = repo.revwalk()?;
    walk.set_sorting(Sort::TOPOLOGICAL | Sort::TIME)?;
    if let Some(head) = head {
        walk.push(head)?;
    }
    match repo.find_reference(LEGACY_TIMELINE) {
        Ok(reference) => walk.push(reference.peel_to_commit()?.id())?,
        Err(e) if e.code() == ErrorCode::NotFound => {}
        Err(e) => return Err(e),
    }
    walk.map(|id| {
        let id = id?;
        let commit = repo.find_commit(id)?;
        let time = commit.time();
        let date = DateTime::from_timestamp(time.seconds(), 0)
            .zip(
                time.offset_minutes()
                    .checked_mul(60)
                    .and_then(FixedOffset::east_opt),
            )
            .map(|(time, offset)| {
                time.with_timezone(&offset)
                    .format("%Y-%m-%d %H:%M:%S %:z")
                    .to_string()
            })
            .unwrap_or_else(|| "时间不可用".into());
        let mut tags = tags.remove(&id).unwrap_or_default();
        tags.sort_unstable();
        Ok(Version {
            id,
            current: Some(id) == head,
            message: String::from_utf8_lossy(commit.message_bytes())
                .trim()
                .to_owned(),
            date,
            tags,
        })
    })
    .collect()
}

pub fn check_tag_name(repo: &Repository, name: &str) -> Result<(), String> {
    let reference = format!("refs/tags/{}", name.trim());
    if name.trim().is_empty() || !git2::Reference::is_valid_name(&reference) {
        return Err("请输入有效的 Git 标签名".into());
    }
    match repo.find_reference(&reference) {
        Ok(_) => Err("此标签已存在，请使用其他名称".into()),
        Err(e) if e.code() == ErrorCode::NotFound => Ok(()),
        Err(e) => Err(e.to_string()),
    }
}

pub fn tag(repo: &Repository, id: Oid, name: &str) -> Result<(), String> {
    let name = name.trim();
    check_tag_name(repo, name)?;
    let commit = repo.find_commit(id).map_err(|e| e.to_string())?;
    repo.tag_lightweight(name, commit.as_object(), false)
        .map_err(|e| e.to_string())?;
    Ok(())
}

pub fn restore(repo: &Repository, id: Oid, destination: &Path) -> Result<PathBuf, String> {
    let destination = destination
        .canonicalize()
        .map_err(|e| format!("无法打开恢复目录：{e}"))?;
    for protected in [repo.workdir(), Some(repo.path())].into_iter().flatten() {
        let protected = protected.canonicalize().map_err(|e| e.to_string())?;
        if destination.starts_with(protected) {
            return Err("请选择当前工作区和 Git 目录之外的目录".into());
        }
    }
    let tree = repo
        .find_commit(id)
        .and_then(|commit| commit.tree())
        .map_err(|e| e.to_string())?;
    let output = tempfile::Builder::new()
        .prefix(&format!("OfficeGitSync-{}-", &id.to_string()[..8]))
        .tempdir_in(destination)
        .map_err(|e| format!("无法创建恢复目录：{e}"))?;
    write_tree(repo, &tree, output.path())?;
    Ok(output.keep())
}

fn safe_name(name: Option<&str>) -> Result<&str, String> {
    let name = name.ok_or("历史版本包含无法解码的文件名")?;
    let mut components = Path::new(name).components();
    if !matches!(components.next(), Some(Component::Normal(part)) if part == name)
        || components.next().is_some()
        || name.contains(['/', '\\', ':'])
        || name.eq_ignore_ascii_case(".git")
    {
        return Err(format!("历史版本包含不安全的路径：{name}"));
    }
    #[cfg(windows)]
    {
        let base = name
            .split('.')
            .next()
            .unwrap_or_default()
            .trim_end()
            .to_ascii_uppercase();
        if name.ends_with(['.', ' '])
            || name
                .chars()
                .any(|c| c.is_control() || "<>\"|?*".contains(c))
            || matches!(
                base.as_str(),
                "CON" | "PRN" | "AUX" | "NUL" | "CONIN$" | "CONOUT$"
            )
            || ((base.starts_with("COM") || base.starts_with("LPT"))
                && matches!(
                    base.get(3..),
                    Some(
                        "0" | "1" | "2" | "3" | "4" | "5" | "6" | "7" | "8" | "9" | "¹" | "²" | "³"
                    )
                ))
        {
            return Err(format!("历史版本包含 Windows 保留路径：{name}"));
        }
    }
    Ok(name)
}

fn write_tree(repo: &Repository, tree: &Tree<'_>, directory: &Path) -> Result<(), String> {
    for entry in tree {
        let name = safe_name(entry.name())?;
        let path = directory.join(name);
        match entry.filemode() {
            0o040000 => {
                fs::create_dir(&path).map_err(|e| format!("{name}：{e}"))?;
                let child = repo.find_tree(entry.id()).map_err(|e| e.to_string())?;
                write_tree(repo, &child, &path)?;
            }
            mode @ (0o100644 | 0o100755) => {
                let blob = repo.find_blob(entry.id()).map_err(|e| e.to_string())?;
                let mut options = OpenOptions::new();
                options.write(true).create_new(true);
                #[cfg(unix)]
                {
                    use std::os::unix::fs::OpenOptionsExt;
                    options.mode((mode & 0o777) as u32);
                }
                #[cfg(not(unix))]
                let _ = mode;
                options
                    .open(&path)
                    .and_then(|mut file| file.write_all(blob.content()))
                    .map_err(|e| format!("{name}：{e}"))?;
            }
            _ => return Err(format!("不能安全导出符号链接、子模块或特殊文件：{name}")),
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::{backup, filters::FilterSettings};
    use std::collections::HashSet;

    #[test]
    fn restores_old_snapshot_without_touching_worktree_or_refs() {
        let dir = tempfile::tempdir().unwrap();
        let checkout = dir.path().join("work");
        let repo = Repository::init(&checkout).unwrap();
        let name = "文档.txt";
        let path = checkout.join(name);
        let selected = HashSet::from([name.to_owned()]);
        let filters = FilterSettings::default();
        fs::write(&path, "历史").unwrap();
        let old = backup::commit(
            &repo,
            &backup::revision(&repo).unwrap(),
            &filters,
            &selected,
            "旧版",
        )
        .unwrap();
        fs::write(&path, "更新").unwrap();
        let new = backup::commit(
            &repo,
            &backup::revision(&repo).unwrap(),
            &filters,
            &selected,
            "新版",
        )
        .unwrap();
        fs::write(&path, "尚未备份").unwrap();

        tag(&repo, old, "old").unwrap();
        assert!(tag(&repo, new, "old").is_err());
        assert_eq!(versions(&repo).unwrap()[1].tags, ["old"]);
        assert!(restore(&repo, old, &checkout).is_err());
        let first = restore(&repo, old, dir.path()).unwrap();
        let second = restore(&repo, old, dir.path()).unwrap();
        assert_ne!(first, second);
        assert_eq!(fs::read_to_string(first.join(name)).unwrap(), "历史");
        assert_eq!(fs::read_to_string(path).unwrap(), "尚未备份");
        assert_eq!(repo.head().unwrap().target(), Some(new));
    }

    #[test]
    fn rejects_unsafe_tree_names() {
        for name in ["..", ".git", ".GIT", "a/b", "a\\b", "C:stream"] {
            assert!(safe_name(Some(name)).is_err(), "{name}");
        }
        assert!(safe_name(Some("报告.txt")).is_ok());
    }

    #[test]
    fn failed_export_publishes_no_partial_snapshot() {
        let dir = tempfile::tempdir().unwrap();
        let workdir = dir.path().join("work");
        let repo = Repository::init(&workdir).unwrap();
        let destination = dir.path().join("exports");
        fs::create_dir(&destination).unwrap();
        let existing = destination.join("保留.txt");
        fs::write(&existing, "保留内容").unwrap();
        let blob = repo.blob(b"target").unwrap();
        let mut builder = repo.treebuilder(None).unwrap();
        builder.insert("a.txt", blob, 0o100644).unwrap();
        builder.insert("z.link", blob, 0o120000).unwrap();
        let tree = repo.find_tree(builder.write().unwrap()).unwrap();
        let sig = git2::Signature::now("Test", "test@example.invalid").unwrap();
        let id = repo
            .commit(None, &sig, &sig, "导出测试", &tree, &[])
            .unwrap();
        assert!(restore(&repo, id, &destination)
            .unwrap_err()
            .contains("符号链接"));
        assert_eq!(fs::read_dir(destination).unwrap().count(), 1);
        assert_eq!(fs::read_to_string(existing).unwrap(), "保留内容");
    }
}
