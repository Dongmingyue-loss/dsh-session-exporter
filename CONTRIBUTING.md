# Contributing

Thanks for improving `dsh-session-exporter`.

## Development setup

```bash
git clone https://github.com/Dongmingyue-loss/dsh-session-exporter.git
cd dsh-session-exporter
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements-dev.txt   # Windows
# .venv/bin/python -m pip install -r requirements-dev.txt     # macOS / Linux
python -m unittest discover -s test -v
```

Runtime core uses only the Python standard library. DSH zstd support uses the Python 3.14+ standard library, or the optional `zstandard` package on older versions.

## Before opening a pull request

1. Run the unit and API tests:

   ```bash
   python -m unittest discover -s test -v
   ```

2. Run the syntax/undefined-name checks:

   ```bash
   ruff check --select E9,F .
   ```

3. For UI changes, run the Playwright smoke test:

   ```bash
   python scripts/make_demo_sessions.py
   python test/ui_smoke.py
   ```

4. Do not commit real DSH sessions, exported conversations, API keys, cache databases, or screenshots containing real data. Use `demo/dsh-sessions` and synthetic fixtures only.

## Pull request expectations

- Keep changes focused.
- Add or update tests for behavior changes.
- Update `README.md` / `docs/` for user-visible changes.
- Preserve the read-only, local-only security model.
