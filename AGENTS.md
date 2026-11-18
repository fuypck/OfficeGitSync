# OfficeGitSync 发布收尾

每次版本发布完成并核验 GitHub Release 后，将对应 OfficeGitSync 开发会话的原始 Codex JSONL 记录同步到本地 `.codex-history/`。先按会话 ID 和工作目录确认来源，再按原文件名更新已有快照，核对字节和完整记录数量，并在报告中注明同步时间。原始文件仍保留在 Codex 会话目录，方便通过 `codex resume` 继续。确保 `.codex-history/` 由 Git 忽略，备份仅保存在本地。
