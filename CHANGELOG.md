# Changelog

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
