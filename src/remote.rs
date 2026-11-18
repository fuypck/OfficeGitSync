use git2::{
    ErrorCode, PushOptions, RemoteCallbacks, Repository, RepositoryInitOptions, RepositoryOpenFlags,
};
use std::{
    fs,
    path::{Path, PathBuf},
};

pub enum Target {
    Url(String),
    Bare(String),
    Worktree(String),
    Empty(PathBuf),
}

impl Target {
    fn address(&self) -> Result<&str, String> {
        match self {
            Self::Url(address) | Self::Bare(address) | Self::Worktree(address) => Ok(address),
            Self::Empty(_) => Err("目标是空目录，请先确认初始化裸仓库".into()),
        }
    }
}

pub fn inspect(repo: &Repository, input: &str) -> Result<Target, String> {
    let input = input.trim();
    if input.is_empty() {
        return Err("请输入 Git 地址或选择已有目录".into());
    }
    let path = if input.starts_with("file://") {
        url::Url::parse(input)
            .ok()
            .and_then(|u| u.to_file_path().ok())
            .ok_or("无法解析本地 file URL；网络共享请使用 UNC 路径或已挂载的目录")?
    } else if is_network_address(input) {
        return Ok(Target::Url(input.into()));
    } else {
        #[cfg(not(windows))]
        if input.starts_with(r"\\") || is_windows_drive(input) {
            return Err("Linux 上请先挂载该网络共享，再输入挂载目录".into());
        }
        let path = Path::new(input);
        #[cfg(windows)]
        if !path.is_absolute() && (path.has_root() || is_windows_drive(input)) {
            return Err("请使用带盘符的绝对路径或完整 UNC 共享路径".into());
        }
        repo.workdir().ok_or("当前仓库没有工作区")?.join(path)
    };
    let path = path
        .canonicalize()
        .map_err(|e| format!("无法访问目标目录 {}：{e}", path.display()))?;
    if !path.is_dir() {
        return Err(format!("目标不是目录：{}", path.display()));
    }
    let workdir = repo
        .workdir()
        .ok_or("当前仓库没有工作区")?
        .canonicalize()
        .map_err(|e| format!("无法核对工作区：{e}"))?;
    if path.starts_with(&workdir) {
        return Err("目标目录位于当前工作区内，请选择工作区之外的目录".into());
    }

    match Repository::open_ext(
        &path,
        RepositoryOpenFlags::NO_SEARCH,
        std::iter::empty::<&str>(),
    ) {
        Ok(target) => {
            let source_git = repo.path().canonicalize().map_err(|e| e.to_string())?;
            let target_git = target.path().canonicalize().map_err(|e| e.to_string())?;
            if source_git == target_git {
                return Err("目标与当前仓库相同，请选择另一个仓库".into());
            }
            let address = local_address(&path)?;
            if target.is_bare() {
                Ok(Target::Bare(address))
            } else {
                Ok(Target::Worktree(address))
            }
        }
        Err(e) if e.code() == ErrorCode::NotFound => {
            let mut entries = fs::read_dir(&path).map_err(|e| format!("无法检查目标目录：{e}"))?;
            if entries
                .next()
                .transpose()
                .map_err(|e| e.to_string())?
                .is_some()
            {
                return Err(format!(
                    "目标是非空普通目录或不完整的 Git 仓库，无法安全初始化：{}",
                    path.display()
                ));
            }
            if path.parent().is_none()
                || path
                    .parent()
                    .is_some_and(|parent| Repository::discover(parent).is_ok())
            {
                return Err("不能在磁盘根目录或现有仓库内初始化远程裸仓库".into());
            }
            Ok(Target::Empty(path))
        }
        Err(e) => Err(format!("无法读取目标 Git 仓库 {}：{e}", path.display())),
    }
}

fn is_windows_drive(input: &str) -> bool {
    matches!(input.as_bytes(), [letter, b':', ..] if letter.is_ascii_alphabetic())
}

fn is_network_address(input: &str) -> bool {
    !is_windows_drive(input)
        && !input.starts_with(r"\\")
        && (input.contains("://")
            || input.split_once(':').is_some_and(|(host, path)| {
                !host.is_empty() && !host.contains(['/', '\\']) && !path.is_empty()
            }))
}

fn local_address(path: &Path) -> Result<String, String> {
    let text = path.to_str().ok_or("目标路径不是有效的 Unicode")?;
    #[cfg(windows)]
    {
        // Windows canonicalize emits extended prefixes. libgit2's local transport
        // recognizes ordinary drive paths and UNC paths with forward slashes.
        let text = text.replace('\\', "/");
        if let Some(unc) = text.strip_prefix("//?/UNC/") {
            return Ok(format!("//{unc}"));
        }
        if let Some(drive) = text.strip_prefix("//?/") {
            if !is_windows_drive(drive) {
                return Err("此 Windows 卷路径不受支持，请使用盘符或 UNC 路径".into());
            }
            Ok(drive.to_owned())
        } else {
            Ok(text)
        }
    }
    #[cfg(not(windows))]
    Ok(text.to_owned())
}

pub fn save(repo: &Repository, name: &str, target: &Target) -> Result<(), String> {
    let address = target.address()?;
    match repo.find_remote(name) {
        Ok(_) => repo
            .remote_set_url(name, address)
            .map_err(|e| e.to_string()),
        Err(e) if e.code() == ErrorCode::NotFound => repo
            .remote(name, address)
            .map(|_| ())
            .map_err(|e| e.to_string()),
        Err(e) => Err(e.to_string()),
    }
}

pub fn initialize(
    repo: &Repository,
    name: &str,
    input: &str,
    confirmed: &Path,
) -> Result<(), String> {
    if !git2::Remote::is_valid_name(name) {
        return Err("远程仓库名称无效".into());
    }
    match inspect(repo, input)? {
        Target::Empty(path) if path == confirmed => {
            let head = repo.find_reference("HEAD").map_err(|e| e.to_string())?;
            let branch = head
                .symbolic_target()
                .and_then(|name| name.strip_prefix("refs/heads/"))
                .ok_or("请先切换到本地分支")?;
            let mut options = RepositoryInitOptions::new();
            options
                .bare(true)
                .no_reinit(true)
                .mkdir(false)
                .mkpath(false)
                .external_template(false)
                .initial_head(branch);
            Repository::init_opts(&path, &options)
                .map_err(|e| format!("无法初始化 {}：{e}", path.display()))?;
            save(repo, name, &Target::Bare(local_address(&path)?))
                .map_err(|e| format!("裸仓库已创建，但登记 Remote 失败：{e}"))
        }
        _ => Err("目标已变化，请重新检查目录后再确认".into()),
    }
}

fn action_remote<'a>(
    repo: &'a Repository,
    name: &str,
    draft: &str,
) -> Result<(git2::Remote<'a>, Target), String> {
    let remote = repo
        .find_remote(name)
        .map_err(|e| format!("请先保存 Remote：{e}"))?;
    let saved = remote.url().ok_or("已登记的远程地址无效")?;
    let target = inspect(repo, saved)?;
    let address = target.address()?;
    let draft = draft.trim();
    let configured = repo
        .config()
        .and_then(|config| config.get_string(&format!("remote.{name}.url")));
    if !draft.is_empty()
        && configured.as_deref().ok() != Some(draft)
        && inspect(repo, draft)?.address()? != address
    {
        return Err("输入地址与已登记的 Remote 不一致，请先保存远程地址".into());
    }
    Ok((remote, target))
}

pub fn fetch(repo: &Repository, name: &str, draft: &str) -> Result<(), String> {
    let (mut remote, target) = action_remote(repo, name, draft)?;
    let address = target.address()?;
    if remote.url() == Some(address) {
        return remote
            .fetch(&[] as &[&str], None, None)
            .map_err(|e| e.to_string());
    }
    // Preserve the configured fetch refspecs when an older local path needs normalization.
    let refspecs = remote.fetch_refspecs().map_err(|e| e.to_string())?;
    let refspecs: Vec<_> = refspecs
        .iter()
        .collect::<Option<_>>()
        .ok_or("远程 Fetch 规则无法解码")?;
    repo.remote_anonymous(address)
        .map_err(|e| e.to_string())?
        .fetch(&refspecs, None, None)
        .map_err(|e| e.to_string())
}

pub fn push_local(repo: &Repository, name: &str, draft: &str) -> Result<(), String> {
    let (mut remote, target) = action_remote(repo, name, draft)?;
    // Use a fresh anonymous remote so a configured push URL cannot redirect this write.
    if !matches!(target, Target::Bare(_)) {
        return Err("此操作只允许推送到本地或已挂载网络上的裸仓库".into());
    }
    let push_url = remote
        .pushurl()
        .unwrap_or(remote.url().ok_or("已登记的远程地址无效")?);
    if inspect(repo, push_url)?.address()? != target.address()? {
        return Err("此 Remote 配置了不同的推送地址，请先在 Git 客户端核对".into());
    }
    remote = repo
        .remote_anonymous(target.address()?)
        .map_err(|e| e.to_string())?;
    if remote.url() != Some(target.address()?)
        || remote
            .pushurl()
            .is_some_and(|url| Some(url) != remote.url())
    {
        return Err("Git 配置重写了裸仓库地址，请先核对 URL 重写规则".into());
    }
    let head = repo
        .head()
        .map_err(|e| format!("当前分支还没有提交：{e}"))?;
    let branch = head
        .name()
        .filter(|name| name.starts_with("refs/heads/"))
        .ok_or("请切换到本地分支后再推送")?;
    let spec = format!("{branch}:{branch}");
    let mut callbacks = RemoteCallbacks::new();
    callbacks.push_update_reference(|reference, status| match status {
        Some(status) => Err(git2::Error::from_str(&format!("{reference}：{status}"))),
        None => Ok(()),
    });
    let mut options = PushOptions::new();
    options.remote_callbacks(callbacks);
    remote
        .push(&[&spec], Some(&mut options))
        .map_err(|e| e.to_string())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn commit(repo: &Repository, message: &str) -> git2::Oid {
        fs::write(repo.workdir().unwrap().join("记录.txt"), message).unwrap();
        let mut index = repo.index().unwrap();
        index.add_path(Path::new("记录.txt")).unwrap();
        index.write().unwrap();
        let tree = repo.find_tree(index.write_tree().unwrap()).unwrap();
        let sig = git2::Signature::now("Test", "test@example.invalid").unwrap();
        repo.commit(Some("HEAD"), &sig, &sig, message, &tree, &[])
            .unwrap()
    }

    #[test]
    fn initializes_only_confirmed_empty_folder_and_round_trips_local_changes() {
        let dir = tempfile::tempdir().unwrap();
        let source = Repository::init(dir.path().join("source")).unwrap();
        let checkout = Repository::init(dir.path().join("reader")).unwrap();
        let target = dir.path().join("共享 备份.git");
        fs::create_dir(&target).unwrap();
        let relative = "../共享 备份.git";
        let Target::Empty(confirmed) = inspect(&source, relative).unwrap() else {
            panic!("expected empty directory");
        };
        assert!(save(&source, "backup", &Target::Empty(confirmed.clone())).is_err());
        assert!(initialize(&source, "backup", relative, dir.path()).is_err());
        assert!(!target.join("HEAD").exists());

        fs::write(target.join("keep.txt"), "keep").unwrap();
        assert!(initialize(&source, "backup", relative, &confirmed).is_err());
        assert_eq!(fs::read_to_string(target.join("keep.txt")).unwrap(), "keep");
        fs::remove_file(target.join("keep.txt")).unwrap();
        initialize(&source, "backup", relative, &confirmed).unwrap();
        let bare = Repository::open_bare(&target).unwrap();
        assert!(bare.is_bare());
        let oid = commit(&source, "first");
        let branch = source.head().unwrap().shorthand().unwrap().to_owned();
        push_local(&source, "backup", relative).unwrap();
        assert_eq!(bare.head().unwrap().target(), Some(oid));

        let file_url = url::Url::from_directory_path(&target).unwrap();
        let remote_target = inspect(&checkout, file_url.as_str()).unwrap();
        save(&checkout, "backup", &remote_target).unwrap();
        fetch(&checkout, "backup", file_url.as_str()).unwrap();
        let track = checkout
            .find_reference(&format!("refs/remotes/backup/{branch}"))
            .unwrap();
        assert_eq!(track.target(), Some(oid));

        commit(&checkout, "unrelated");
        assert!(push_local(&checkout, "backup", "").is_err());
        assert_eq!(bare.head().unwrap().target(), Some(oid));
    }

    #[test]
    fn protects_worktree_and_preserves_network_remote_addresses() {
        let dir = tempfile::tempdir().unwrap();
        let source = Repository::init(dir.path().join("source")).unwrap();
        let target = Repository::init(dir.path().join("target")).unwrap();
        commit(&target, "existing");
        let input = target.workdir().unwrap().to_str().unwrap();
        let target = inspect(&source, input).unwrap();
        assert!(matches!(target, Target::Worktree(_)));
        save(&source, "worktree", &target).unwrap();
        fetch(&source, "worktree", input).unwrap();
        assert!(push_local(&source, "worktree", input).is_err());
        assert!(inspect(&source, ".").is_err());
        assert!(fetch(
            &source,
            "worktree",
            "https://github.com/example/changed.git"
        )
        .is_err());

        for input in [
            "https://github.com/example/git.git",
            "git@github.com:example/git.git",
            "ssh://git@example.com/git.git",
        ] {
            let target = inspect(&source, input).unwrap();
            assert!(matches!(&target, Target::Url(url) if url == input));
            save(&source, "origin", &target).unwrap();
            assert_eq!(source.find_remote("origin").unwrap().url(), Some(input));
        }
    }

    #[cfg(windows)]
    #[test]
    fn preserves_windows_drive_and_unc_semantics() {
        assert!(is_windows_drive(r"C:\Git Repo"));
        assert!(!is_network_address(r"C:\Git Repo"));
        assert!(!is_network_address(r"\\server\share\Git Repo"));
        assert_eq!(
            local_address(Path::new(r"\\?\UNC\server\share\Git Repo")).unwrap(),
            "//server/share/Git Repo"
        );
        assert_eq!(
            local_address(Path::new(r"\\?\C:\Git Repo")).unwrap(),
            "C:/Git Repo"
        );
    }
}
