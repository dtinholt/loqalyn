# Loqalyn: rename and upgrade notes

Release: **0.3.0-alpha.3**, October 14, 2026. Former name: Switchyard.

Loqalyn is an independent local AI workspace. The rename avoids confusion with [NVIDIA NeMo Switchyard](https://github.com/NVIDIA-NeMo/Switchyard), a separate model-routing project. No affiliation is implied. The MIT license and project history are retained.

## Name screening

Before publication, exact-name web searches found no identifiable app, software product or company using **Loqalyn**. GitHub repository search returned zero matches; the PyPI and npm package endpoints returned 404; Hugging Face model search and the US Mac App Store search returned no matches. Verisign's `.com` RDAP lookup returned 404. This records a point-in-time public screening, not legal trademark clearance, an exhaustive worldwide search, or a domain reservation. The domain was not purchased. See [the recorded checks](evidence/rename-name-check.json).

## Upgrade

1. Quit the earlier Switchyard app so it releases the local port.
2. Install **Loqalyn.app** from this release in Applications (or your personal Applications folder).
3. Open Loqalyn. Existing settings, API token, model catalogue and history are used automatically. Ollama/LM Studio continue to own their existing model files.
4. Keep the old app as a rollback until the new release is working. Run only one version at a time.

Existing desktop profiles remain in `~/Library/Application Support/Switchyard`; this avoids copying secrets, resetting privacy settings, or splitting journal history. Fresh installs use `~/Library/Application Support/Loqalyn`. When both folders exist, the new folder takes precedence. The data-folder button shows the folder actually in use. Nothing is moved or deleted by the rename.

Standalone CLI profiles follow the same rule for `~/.switchyard` and `~/.loqalyn`. An explicit `--data-dir` wins over environment defaults. `LOQALYN_DIR`, `LOQALYN_PORT`, and `LOQALYN_LM_TOKEN` take precedence over their `SWITCHYARD_*` equivalents. The `yard` command remains an alias when installed from source. Existing Keychain entries are read only as a fallback; newly saved credentials use the `loqalyn` service.

The native bundle identifier is now `dev.loqalyn.desktop`. The Python package and primary command are `loqalyn`. Source integrations must update Python imports. The base inference endpoint remains `http://127.0.0.1:8741/v1`. Control paths use `/loqalyn/*`, with `/switchyard/*` retained as aliases. Both old and new receipt headers are emitted. Security checks apply equally to both prefixes.

## Release checks

- **108 automated tests passed**, including legacy-profile startup, token/policy preservation, environment precedence, Keychain fallback, old/new API authentication, and matching generation receipts. [Test output](evidence/rename-tests.txt).
- **43,240 simulated requests**, including overload, injected upstream failures, 40 disconnects and a 60-second soak: zero unexpected outcomes, zero active-model evictions, full recovery, and zero active requests at completion. Gateway peak RSS was 59.48 MiB, excluding model runtimes. [Stress report](evidence/rename-stress.json).
- **Six real local Qwen completions** passed text, streaming, JSON and repeat checks; the pre-existing loaded model was preserved. [Inference report](evidence/rename-live-ollama.json).
- **26 locked dependency versions** returned no published PyPI advisories and no query errors. This is a point-in-time check, not proof that no vulnerabilities exist. [Advisory results](evidence/rename-dependency-advisories.json).
- Clean ZIP extraction, bundled CLI startup and strict nested-signature verification passed before and after launch. Every bundled application module matches the published source. [Package checks](evidence/rename-package.json), [source hashes](evidence/rename-app-source-sha256.json).
- The installed native app opens as **Loqalyn**, displays Discover and retrieves 20 live Hugging Face search results. Existing profile/token fingerprints are unchanged; local-only mode and both API prefixes work. [Installation checks](evidence/rename-installation.json).

Historical validation files retain the old product name and measurements; they have not been relabeled as fresh results. This rename does not extend the inference formats, hardware support or security certification of Alpha 2. The Mac bundle remains ad-hoc signed, without Apple Developer ID signing or notarization.
