# Changelog

## v1.0.0

Initial release.

- Read-only access to DSH / DSH Desktop Beta `session.vN.jsonl[.zstd]` logs.
- Automatic discovery of `~/.dsh-beta/sessions`, `~/.dsh/sessions`, `DSH_SESSIONS_DIR`, and `--dsh-sessions`.
- Native zstd decoding: Python 3.14+ standard library, optional `zstandard` on Python 3.9-3.13.
- Normalization of user/assistant/system/developer messages, tool calls/results, attachments, model changes, compaction, and error notices.
- Three surface event modes: `append`, `append+replace`, and `replace`.
- Local web GUI with source tabs, session search/sorting, and paged message preview.
- Readable TXT export with reasoning, tool input/output, file references, timeline, compaction, and message metadata.
- Normalized JSONL export with message and part metadata.
- Paged export preview with previous/next/jump navigation.
- Custom User / Agent display names for readable TXT.
- Streaming export with progress, cancellation, split files, and ZIP packaging.
- SQLite normalization cache owned by the exporter; DSH files are never modified.
- Cross-platform launchers: `start.cmd` (Windows) and `start.sh` (macOS/Linux).
- Bundled synthetic DSH v4 demo sessions.
