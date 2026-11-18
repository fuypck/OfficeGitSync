fn main() {
    println!("cargo:rerun-if-env-changed=OFFICEGITSYNC_VERSION");
    let version = std::env::var("OFFICEGITSYNC_VERSION")
        .unwrap_or_else(|_| env!("CARGO_PKG_VERSION").to_owned());
    println!("cargo:rustc-env=OFFICEGITSYNC_VERSION={version}");
    #[cfg(windows)] {
        let numeric = version.trim_start_matches('v');
        let mut res = winres::WindowsResource::new();
        res.set("ProductName", "OfficeGitSync");
        res.set("ProductVersion", numeric);
        res.set("FileVersion", numeric);
        res.compile().expect("Windows resource failed");
    }
}
