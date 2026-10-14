# Switchyard

One local endpoint for models in Ollama and LM Studio, with a native macOS interface, explainable routing, coordinated model loading, and individually reviewed cloud escalation.

**0.3.0-alpha.1 · Apple silicon · MIT**

This is a testable alpha, not the full v1 hardware/quality certification described in the historical PRD. See [validation evidence](docs/VALIDATION.md), [supported API](docs/API.md), and [security boundaries](SECURITY.md).

## Install the Mac app

1. Download `Switchyard-0.3.0-alpha.1-Mac-Apple-Silicon.zip` from [Releases](https://github.com/dtinholt/switchyard/releases).
2. Extract it and move **Switchyard.app** to Applications. Python is included; no terminal installer is needed.
3. Open Switchyard. This alpha is ad-hoc signed, without Apple Developer ID signing or notarization. macOS may require you to review it in **System Settings → Privacy & Security → Open Anyway**. Only approve the download you intended to install; don't disable Gatekeeper globally.
4. Start Ollama or LM Studio separately. In Switchyard **Settings**, enable the runtimes you use and check their local addresses. Default addresses are `http://127.0.0.1:11434` and `http://127.0.0.1:1234`.
5. Disable runtime cloud/remote forwarding, then confirm local-only setup in Settings. For Ollama see its [cloud controls](https://docs.ollama.com/faq#how-do-i-disable-ollamas-cloud-features). For LM Studio, use local models with remote/Link features disabled. A loopback address alone cannot prove that the runtime never forwards requests.
6. Choose **Discover**, then try **Playground → Evaluate route** and generate a short response.

Requires an Apple silicon Mac; the bundle targets macOS 13+, but local validation was on macOS 27.0.1. Older supported targets still need on-device acceptance. Inference requires a separately installed runtime and suitable local model. Model files are not included.

The first-run memory budget is 75% of detected RAM, capped at 96 GiB, with reserved headroom. On a 32 GB Mac this starts at 24 GiB with 4 GiB headroom. These are estimates, not OS-wide memory guarantees. Begin with a small model; do not run simultaneous heavy workloads in other apps during evaluation.

## Use it

- **Overview:** runtime health and estimated model reservations.
- **Models:** discover, configure, load, unload, and explicitly download models. Runtime-advertised tool support is not verification: mark tools verified only after testing that model.
- **Playground:** preview a route or generate locally. Tool calls are displayed, never executed by the app.
- **Policies:** edit routes, preferences, and admission settings.
- **Activity:** inspect request outcomes and export metadata without prompts or generated text.
- **Escalations:** review/redact the entire outgoing request, preview the exact provider payload, and approve one send. Nothing is sent automatically.
- **Settings:** memory budget, runtime addresses, local-only confirmation, and optional provider credentials in macOS Keychain.

The gateway listens only on `127.0.0.1:8741`. Copy the local API token from Settings into your client. Treat it like a password: it authorizes generation and configuration, not just reading status. The only unauthenticated endpoint is `/health`. Closing the window keeps the menu-bar service running; **Quit Switchyard** stops the owned gateway without quitting the runtimes.

Client base URL: `http://127.0.0.1:8741/v1`. Model: `auto`, `auto-fast`, `auto-code`, or a runtime-qualified model ID. The response identifies the model that actually served it. For a conversation or tool cycle, pin that returned model ID to retain model continuity; `auto` routes each request independently.

## Source, CLI, and tests

Python 3.12+:

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[mac]'  # on macOS; use -e . for the gateway on Linux
python -m unittest discover -v
python scripts/stress_gateway.py --output validation-output/stress
```

The desktop is macOS-only. Linux gateway CI is a portability check, not a supported desktop distribution. Development installation resolves platform dependencies; the release bundle instead uses the complete URL/SHA-256 lock in `macos/dependencies.lock.json`.

```sh
python -m switchyard.desktop                 # native interface
# Alternatively, without starting a second listener:
yard --data-dir "$HOME/Library/Application Support/Switchyard" status
yard --data-dir "$HOME/Library/Application Support/Switchyard" route 'Explain a Python function'
yard --data-dir "$HOME/Library/Application Support/Switchyard" mcp
```

Standalone `yard serve` uses `~/.switchyard` unless `--data-dir` or `SWITCHYARD_DIR` is supplied. Native app data lives in `~/Library/Application Support/Switchyard`. Do not run both on the same port. The app also includes the CLI at `/Applications/Switchyard.app/Contents/MacOS/Switchyard --cli` and uses its native data directory automatically.

MCP exposes `list_models`, `route`, `load`, and `escalation_status` over stdio. It has no cloud-approval or tool-execution method. See [API and compatibility](docs/API.md).

## Build and contribute

[Build instructions](docs/BUILD.md) cover the complete launcher, bundled Python, locked dependencies, code signing, and packaging. [Architecture](docs/ARCHITECTURE.md) describes ownership and request flow. [Research](docs/ROUTER-RESEARCH.md) explains practices adopted from other routers without copying their code.

Run the tests before submitting changes. Add focused regression coverage for routing, security, or lifecycle fixes. Never commit model weights, local profiles, credentials, prompts, or user logs. The app's [MIT license](LICENSE) is separate from bundled dependencies' [notices](THIRD-PARTY-NOTICES.md).
