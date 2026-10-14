# Loqalyn

**Your models. Your machine.**

One local endpoint for models in Ollama and LM Studio, with a native macOS interface, explainable routing, coordinated model loading, and individually reviewed cloud escalation.

**0.4.0-alpha.1 · Apple silicon · MIT**

This is a testable alpha, not the full v1 hardware/quality certification described in the historical PRD. See [current validation](docs/CLAUDE-VALIDATION.md), [historical evidence](docs/VALIDATION.md), [supported API](docs/API.md), and [security boundaries](SECURITY.md).

Loqalyn was previously released as Switchyard. It is an independent project, with no affiliation to NVIDIA NeMo Switchyard. See [the rename and upgrade notes](docs/RENAME.md).

## Install the Mac app

1. Download `Loqalyn-0.4.0-alpha.1-Mac-Apple-Silicon.zip` from [Releases](https://github.com/dtinholt/loqalyn/releases).
2. Extract it and move **Loqalyn.app** to Applications. Python is included; no terminal installer is needed.
3. Open Loqalyn. This alpha is ad-hoc signed, without Apple Developer ID signing or notarization. macOS may require you to review it in **System Settings → Privacy & Security → Open Anyway**. Only approve the download you intended to install; don't disable Gatekeeper globally.
4. Start Ollama or LM Studio separately. In Loqalyn **Settings**, enable the runtimes you use and check their local addresses. Default addresses are `http://127.0.0.1:11434` and `http://127.0.0.1:1234`.
5. Disable runtime cloud/remote forwarding. For an Ollama-only setup, choose **Settings → Verify local setup**; supported Ollama versions can confirm the setting automatically. Otherwise confirm it manually. For Ollama see its [cloud controls](https://docs.ollama.com/faq#how-do-i-disable-ollamas-cloud-features). For LM Studio, use local models with remote/Link features disabled. A loopback address alone cannot prove that the runtime never forwards requests.
6. Installed models appear in **Models**. To add one, use **Discover → Search Hugging Face → Inspect files → Download & install**. Then try **Playground → Evaluate route** and generate a short response.

Requires an Apple silicon Mac; the bundle targets macOS 13+, but local validation was on macOS 27.0.1. Older supported targets still need on-device acceptance. Inference requires a separately installed runtime and suitable local model. Model files are not included.

New profiles enable Ollama by default. Enable LM Studio separately in Settings if you use it; existing profiles retain their choices.

The first-run memory budget is 75% of detected RAM, capped at 96 GiB, with reserved headroom. On a 32 GB Mac this starts at 24 GiB with 4 GiB headroom. These are estimates, not OS-wide memory guarantees. Begin with a small model; do not run simultaneous heavy workloads in other apps during evaluation.

## Use it

- **Overview:** runtime health and estimated model reservations.
- **Models:** refresh installed models, configure, load, unload, or install a known runtime model ID. Runtime-advertised tool support is not verification: mark tools verified only after testing that model.
- **Discover:** search Hugging Face or enter a publisher/model repository, compare GGUF sizes, review its license, and install into Ollama with progress and cancellation. Only public, ungated, single-file GGUFs with SHA-256 metadata are offered.
- **Playground:** preview a route or generate locally. Answers appear first; model reasoning is optional. Tool calls are displayed, never executed by the app.
- **Claude Code:** check a local model's tool support, choose a project, and open Claude Code in Terminal. Loqalyn chooses a model automatically and keeps it fixed through the task. See [Claude Code setup](docs/CLAUDE-CODE.md).
- **Policies:** edit routes, preferences, and admission settings.
- **Activity:** inspect request outcomes and export metadata without prompts or generated text.
- **Escalations:** review/redact the entire outgoing request, preview the exact provider payload, and approve one send. Nothing is sent automatically.
- **Settings:** memory budget, runtime addresses, local-only confirmation, and optional provider credentials in macOS Keychain.

The gateway listens only on `127.0.0.1:8741`. Copy the local API token from Settings into your client. Treat it like a password: it authorizes generation and configuration, not just reading status. The only unauthenticated endpoint is `/health`. Closing the window keeps the menu-bar service running; **Quit Loqalyn** stops the owned gateway without quitting the runtimes.

Client base URL: `http://127.0.0.1:8741/v1`. Model: `auto`, `auto-fast`, `auto-code`, or a runtime-qualified model ID. The response identifies the model that actually served it. For a conversation or tool cycle, pin that returned model ID to retain model continuity; `auto` routes each request independently.

The **Claude Code** launcher handles task continuity automatically. It uses a separate local profile and an inference-only credential; your existing Claude Code settings are not changed. Coding models must pass a local tool round trip, have at least 64K context, and fit the available memory budget. Each new task can select a different eligible model; an ongoing task fails explicitly if its pinned model disappears or changes. This release supports text coding through Ollama and Claude Code 2.1.299 or newer, with 2.1.299 tested. It does not require a Claude subscription for local model inference.

## Source, CLI, and tests

Python 3.12+:

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[mac]'  # on macOS; use -e . for the gateway on Linux
python -m unittest discover -v
python scripts/stress_gateway.py --output validation-output/stress
```

The desktop is macOS-only. This release was validated on macOS; Linux gateway portability needs separate validation. Development installation resolves platform dependencies; the release bundle instead uses the complete URL/SHA-256 lock in `macos/dependencies.lock.json`.

```sh
python -m loqalyn.desktop                 # native interface
# Alternatively, without starting a second listener:
/Applications/Loqalyn.app/Contents/MacOS/Loqalyn --cli status
/Applications/Loqalyn.app/Contents/MacOS/Loqalyn --cli route 'Explain a Python function'
/Applications/Loqalyn.app/Contents/MacOS/Loqalyn --cli mcp
/Applications/Loqalyn.app/Contents/MacOS/Loqalyn --cli claude /path/to/project
```

Standalone `loqalyn serve` uses `~/.loqalyn` unless `--data-dir` or `LOQALYN_DIR` is supplied. New native app profiles live in `~/Library/Application Support/Loqalyn`. Existing `Switchyard` profiles are reused in place, preserving the API token, policy, catalogue and history. Existing `~/.switchyard` CLI profiles are likewise reused. `SWITCHYARD_*` environment variables and the `yard` command remain compatibility aliases; `LOQALYN_*` takes precedence. Do not run both on the same port. The app also includes the CLI at `/Applications/Loqalyn.app/Contents/MacOS/Loqalyn --cli` and uses its native data directory automatically.

MCP exposes `list_models`, `route`, `load`, and `escalation_status` over stdio. It has no cloud-approval or tool-execution method. See [API and compatibility](docs/API.md).

## Build and contribute

[Build instructions](docs/BUILD.md) cover the complete launcher, bundled Python, locked dependencies, code signing, and packaging. [Architecture](docs/ARCHITECTURE.md) describes ownership and request flow. [Research](docs/ROUTER-RESEARCH.md) explains practices adopted from other routers without copying their code.

Run the tests before submitting changes. Add focused regression coverage for routing, security, or lifecycle fixes. Never commit model weights, local profiles, credentials, prompts, or user logs. The app's [MIT license](LICENSE) is separate from bundled dependencies' [notices](THIRD-PARTY-NOTICES.md).
