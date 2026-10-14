# Changelog

## 0.4.0-alpha.1 — 2026-10-14

- Add native Claude Code setup, local model readiness checks, a two-request tool verification, project selection, and a Terminal launcher with an isolated client profile.
- Automatically select eligible local coding models and persist task locks across tool calls, retries, compaction and gateway restarts. New tasks may choose a different model; missing or changed pinned models fail explicitly.
- Use a separate inference-only credential, preserve Claude Code's tool permissions, filter inherited cloud credentials, and keep coding requests out of cloud escalation.
- Accept bounded Claude Code tool schemas and cache hints; add conservative token estimation, Anthropic-shaped errors, model discovery and stream heartbeats. Reject unsupported thinking, media, hosted tools and forced tool choices.
- Tie coding tool verification to each Ollama manifest revision. Include model readiness reasons and recent task/model locks in the interface.
- Add actual Claude Code client workflow tests, HTTP concurrency and restart tests, security regressions, and reproducible synthetic and real-model validation. See [Claude Code setup and limits](docs/CLAUDE-CODE.md).

## 0.3.0-alpha.3 — 2026-10-14

- Rename the app and public project to **Loqalyn** to avoid confusion with NVIDIA NeMo Switchyard.
- Rename the native application, menus, bundle, Python package, primary CLI, documentation and release artifacts.
- Preserve existing profiles in place, read legacy Keychain credentials when needed, and retain old environment variables, CLI and HTTP API aliases.
- Add upgrade and compatibility regression tests; rebuild and validate the Mac app. See [rename notes](docs/RENAME.md).

## 0.3.0-alpha.2 — 2026-10-14

- Add a native Discover page for public Hugging Face GGUF search, repository inspection, file sizes/licenses, confirmed Ollama install, progress and cancellation.
- Recheck repository revisions and weight digests; persist admission blocks for incomplete installations.
- Check Ollama cloud settings before inference, simplify Ollama-only setup verification and enable only Ollama in new profiles.
- Move network discovery outside the ledger lock, reject stale snapshots, cache bounded manifest metadata, and remove repeated protected-memory scans.
- Honor explicit attention K/V dimensions in memory estimates, including Qwen3.
- Bound stream-error cleanup, enforce JSON object responses and UTF-8 byte limits, and apply log-retention changes during operation.
- Show answers before optional reasoning, throttle streaming redraws and show model family/parameter information and useful memory exclusion details.
- Add targeted regression tests, refreshed stress evidence and a live Hugging Face installation check. See [second review](docs/SECOND-REVIEW.md).

## 0.3.0-alpha.1

Reconstructs a complete, buildable source tree from the supplied 0.2.0 app. The unavailable 0.2.1 source is not a dependency of this release.

- Redesign all seven native screens with a dark sidebar, clear typography, teal actions, rounded cards, readable model details, and response-first playground layout.
- Preserve unsaved policy/settings drafts and reject stale saves instead of overwriting newer settings.
- Fix native Apple silicon callback signatures and prevent runtime cache writes from invalidating the signed app.
- Remove the obsolete token-bearing browser dashboard; authenticate all controls and preserve minimal public health.
- Bound concurrency, payloads, output, JSON/schema work, approval retention, download jobs, and journal history.
- Harden private files, token creation, Host/Origin parsing, runtime metadata/stream validation, and error redaction.
- Use patched APSW/SQLite with defensive database settings; block the old stdlib SQLite entry point in the bundle.
- Recheck admission after queueing, preserve uncertain allocations, handle disconnects, and avoid replaying partial output.
- Bind one-time cloud approval to exact effective content and clear stale native review data.
- Add reproducible score explanations, policy fingerprints, operational counters, fault tests, and a separate-process stress/soak harness.
- Publish MIT source, a complete native builder, dependency hashes/notices, and qualified validation evidence.
