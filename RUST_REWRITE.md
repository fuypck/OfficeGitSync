# OfficeGitSync Rust rewrite

Development branch: `rust-rewrite`. The original Python implementation remains on `main`.

## Current milestone

- Native Rust/egui GUI scaffold
- Open an existing local Git repository and show Git status
- Grouped temporary-file exclusions for Office/WPS, LibreOffice, Vim, Emacs, FreeFileSync, browsers
- Editable custom glob patterns in the prototype interface
- Windows CI on every push to this branch

**Not yet implemented:** file watching, commit confirmation, version history restore, remote fetch/pull/push, credential management, persisted settings, Office-aware diff, and tag-based release builds. Do not use this preview as a backup tool.

## Local development

```sh
cargo run
cargo test
```

The old Python release workflow is intentionally left unchanged on the `main` branch. Once Rust CI passes and release metadata integration is implemented, replace the release workflow on this branch. A tag must point to the Rust commit to publish the Rust executable.
