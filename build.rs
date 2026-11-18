fn main() {
    println!("cargo:rerun-if-env-changed=OFFICEGITSYNC_VERSION");
    let version = std::env::var("OFFICEGITSYNC_VERSION")
        .unwrap_or_else(|_| env!("CARGO_PKG_VERSION").to_owned());
    println!("cargo:rustc-env=OFFICEGITSYNC_VERSION={version}");
    if std::env::var("CARGO_CFG_TARGET_OS").as_deref() == Ok("windows") {
        let numeric = version.trim_start_matches('v');
        println!("cargo:rerun-if-changed=assets/officegitsync.ico");
        let mut res = winres::WindowsResource::new();
        res.set_icon("assets/officegitsync.ico");
        let gnu = std::env::var("CARGO_CFG_TARGET_ENV").as_deref() == Ok("gnu");
        if gnu && std::env::var("HOST") != std::env::var("TARGET") {
            let target = std::env::var("TARGET").expect("Cargo target");
            let prefix = target.replace("-pc-windows-gnu", "-w64-mingw32");
            res.set_toolkit_path(&std::env::var("CARGO_MANIFEST_DIR").expect("manifest directory"));
            res.set_windres_path(&format!("{prefix}-windres"));
            res.set_ar_path(&format!("{prefix}-ar"));
        }
        res.set("ProductName", "OfficeGitSync");
        res.set("ProductVersion", numeric);
        res.set("FileVersion", numeric);
        res.compile().expect("Windows resource failed");
        if gnu {
            // MinGW skips the resource-only archive because it exports no symbols.
            let output = std::env::var("OUT_DIR").expect("Cargo output directory");
            println!("cargo:rustc-link-arg={output}/resource.o");
        }
    }
}
