# Archived development notes

Historical notes from when muxdesk was extracted out of a private monorepo
(`ibkr-trade-journal`). Kept for provenance — **not** current documentation.

- `02-packaging.md` — the original packaging checklist. Superseded by `pyproject.toml`
  (the package is now `pip install muxdesk` / `muxdesk[server]`).
- `03-extraction.md` — the monorepo → standalone extraction strategy and the
  `/api/muxdesk` proxy contract; referenced when migrating the originating monorepo
  onto the published package.

For current docs see the repo `README.md`, `REQUIREMENTS.md`, `CONTRIBUTING.md`,
and `docs/01-pitfalls.md`.
