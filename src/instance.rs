use std::{
    fs::{self, File, OpenOptions, TryLockError},
    io,
    path::{Path, PathBuf},
};

pub struct Instance {
    _lock: File,
    show_file: PathBuf,
}

impl Instance {
    pub fn acquire(directory: &Path) -> io::Result<Option<Self>> {
        fs::create_dir_all(directory)?;
        let lock = OpenOptions::new()
            .write(true)
            .create(true)
            .truncate(false)
            .open(directory.join("instance.lock"))?;
        let show_file = directory.join("show.request");
        match lock.try_lock() {
            Ok(()) => Ok(Some(Self {
                _lock: lock,
                show_file,
            })),
            Err(TryLockError::WouldBlock) => {
                match OpenOptions::new()
                    .write(true)
                    .create_new(true)
                    .open(&show_file)
                {
                    Ok(_) => Ok(None),
                    Err(e) if e.kind() == io::ErrorKind::AlreadyExists => Ok(None),
                    Err(e) => Err(e),
                }
            }
            Err(TryLockError::Error(e)) => Err(e),
        }
    }

    pub fn take_show_request(&self) -> bool {
        fs::remove_file(&self.show_file).is_ok()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn second_instance_requests_show_and_can_start_after_exit() {
        let dir = tempfile::tempdir().unwrap();
        let first = Instance::acquire(dir.path()).unwrap().unwrap();
        assert!(Instance::acquire(dir.path()).unwrap().is_none());
        assert!(Instance::acquire(dir.path()).unwrap().is_none());
        assert!(first.take_show_request());
        assert!(!first.take_show_request());
        drop(first);
        assert!(Instance::acquire(dir.path()).unwrap().is_some());
    }
}
