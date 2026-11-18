# Changelog

## 0.2.1 — Rust preview

- Validate local paths and mounted network shares before saving Git remotes.
- Add native directory browsing, explicit bare-repository initialization, and non-forced pushes to local bare repositories.
- Add a file picker and clearer errors for Python configuration imports.
- Add GitHub and Lanzou links to the settings page.

## 0.2.0 — Rust preview

- Add a four-page Simplified Chinese desktop interface for Windows and Linux.
- Add recursive file monitoring and confirmed, selected-file backups with embedded libgit2.
- Add history, tags, and safe export to a separate directory.
- Add per-user settings, themes, filters, tray controls, and single-instance activation.
- Provide Windows preview builds through Rust CI.

## 0.1.4 — legacy Python

- Limit backup prompts to Git-changed paths involved in the current filesystem event.
- Guard commits against unrelated pre-staged files.
- Repair stale Windows startup commands when the application path changes.
- Exit cleanly when another OfficeGitSync instance is already running.
- Disable unsafe direct commit-history deletion pending a safe implementation.
- Clarify restore behavior and update build/dependency documentation.
