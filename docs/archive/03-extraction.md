# cc-desk — Extraction strategy

cc-desk was born inside a private monorepo (`ibkr-trade-journal`) and lives now as
this standalone repo. This note records the **decided** strategy so the two don't
silently drift.

## Decision

1. **This repo is the single source of truth.** New cc-desk work happens here, not
   in the monorepo.
2. **The monorepo consumes cc-desk as an external service (proxy).** It keeps only:
   - its Vite dev proxy mapping `/api/agent/cc-desk` → cc-desk's standalone backend (`:8001`),
   - its **own** `harness.json` (project-specific quick-action bar), and
   - any project-specific chrome around the embed.
   It does **not** keep a copy of cc-desk's backend/frontend source.
3. **Removal from the monorepo is deferred.** Do not strip cc-desk out of
   `ibkr-trade-journal` until this repo is verified as a drop-in (demo runs,
   requirements green, owner sign-off). Tracked, not yet executed.

## Why proxy (not vendored dependency / submodule)

The standalone backend already runs as its own process on `:8001` and the frontend
talks to it over `/api/agent/cc-desk`. The monorepo's frontend already proxies that
prefix to `:8001`. So "consume as a service" is **the architecture that already
exists** — the least-coupling option:

- The monorepo needs no Python/npm dependency on cc-desk internals.
- cc-desk can version and ship independently.
- The only contract is the HTTP/WS API under `/api/agent/cc-desk` + the
  `harness.json` shape.

A vendored `pip`/`npm` dependency would re-couple versions; a git submodule adds
checkout friction for no benefit over a running service.

## Migration checklist (run only after sign-off)

- [ ] Demo verified here: `./serve.sh demo` passes preflight and drives a session.
- [ ] `REQUIREMENTS.md` acceptance boxes walked and green.
- [ ] Monorepo points its proxy at this repo's `:8001` and works end-to-end.
- [ ] Remove cc-desk source from the monorepo, leaving only proxy config + its
      `harness.json` + embed chrome.
- [ ] Monorepo docs updated to "cc-desk runs as an external service; see
      claude-tmux-desk".

## Public exposure

Repo is private for now. Before flipping to public:

- [ ] Confirm no private residue (paths, tokens, project-specific text); the
      shipped `harness.example.json` is project-neutral.
- [ ] README "Try the demo" vs "Install" paths both make sense to a stranger.
- [ ] `docs/01-pitfalls.md` caveats about Claude Code's undocumented internals are
      stated up front (breakage risk is shared by all tools in this space).
