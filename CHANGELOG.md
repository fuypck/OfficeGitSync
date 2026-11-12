# Changelog

## 0.1.4 — unreleased

- Limit backup prompts to Git-changed paths involved in the current filesystem event.
- Guard commits against unrelated pre-staged files.
- Repair stale Windows startup commands when the application path changes.
- Exit cleanly when another OfficeGitSync instance is already running.
- Disable unsafe direct commit-history deletion pending a safe implementation.
- Clarify restore behavior and update build/dependency documentation.
