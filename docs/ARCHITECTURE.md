# Architecture

The native AppKit interface runs on the main thread. `DesktopController` owns a loopback socket, a private profile lock, and an aiohttp service on a background event loop. A second instance fails without taking over an existing listener. UI work is dispatched to background workers; callbacks return to the main thread.

1. `security.py` enforces bounded JSON, private files, token handling, schema limits, and privacy tags.
2. `server.py` authenticates, limits concurrent work, assigns request records, and translates API responses. It owns short-lived approval/download state.
3. `core.py` validates settings and payloads, ranks eligible models with deterministic rules, persists metadata, and maintains the admission ledger.
4. `adapters.py` translates trusted loopback Ollama/LM Studio protocols into a checked common stream.
5. `desktop_presenter.py` contains pure form/approval/stream presentation logic; `desktop_ui.py` builds the native interface.
6. `cli.py` provides authenticated commands and bounded stdio MCP.

A model lease is acquired before loading/generation and released once, after upstream closure. The ledger serializes mutation across runtimes, protects active/resident/external instances, and retains uncertain reservations after interrupted runtime operations. Admission is rechecked after waiting; a route preview does not reserve memory.

The native executable is a small C launcher that starts included Python with isolated imports and bytecode writing disabled. It runs only bundled source/dependencies. The full launcher, bootstrap, dependency manifest, icon, and app code are included in this repository; no missing cloud workspace is required to rebuild it.

`PRD.md` is the historical 0.1 product brief. This release replaces its browser/Swift-shell assumptions with the native PyObjC application and does not claim its full-v1 acceptance gates.
