For the current **0.3.0-alpha.2** results, see [the second review](SECOND-REVIEW.md). The evidence below is retained for alpha 1.

# Alpha validation — 0.3.0-alpha.1

Validated October 13, 2026 on an Apple silicon Mac with 32 GiB RAM, macOS 27.0.1 and bundled Python 3.12.15. This is a usable, tested alpha with the limitations below. It is not an independent penetration-test certification, a model-quality benchmark, or the historical PRD's full v1 acceptance.

## Results

| Check | Result and evidence |
| --- | --- |
| Automated unit/integration/native bridge suite | **74 passed** in 4.120 seconds on the final application source. [Test log](evidence/tests.txt) |
| Synthetic HTTP stress and recovery | **35,712 attempts**, including expected capacity rejections and deliberately injected upstream failures; no unexpected outcomes. [Report](evidence/stress.json) |
| Real local inference | **6 completions**, all required checks passed: cold text, warm streaming, checked JSON, three repetitions, unload and reservation release. [Report](evidence/live-ollama.json) |
| Native interface | All seven redesigned screens inspected; model selection and protected controls verified; generation completed and appeared in Activity. Earlier interaction checks covered load/unload, cancellation, invalid settings, stale policy protection and local approval denial. [Record](evidence/native-ui.json) |
| Dependency advisories | 26 locked Python distributions queried against PyPI advisory data; no reported advisories or query errors. [Results](evidence/dependency-audit.json) |
| Static security review | Bandit 1.9.4 scanned 3,830 lines across application, bootstrap, builder and scripts. All 38 findings reviewed; see [triage](STATIC-ANALYSIS.md). No scan errors. |
| Packaging | Independent builder inputs verified against SHA-256 lock; nested ad-hoc signatures checked; packaged app cleanly extracted, launched its bundled CLI, and retained a valid signature. [Receipt](evidence/package.json) |
| Release source inspection | No credential-pattern or private-workspace-path matches; source inventory excludes profiles, weights and caches. [Scan](evidence/source-scan.json) |
| C launcher static analysis | Apple Clang analyzer returned zero diagnostics. [Record](evidence/launcher-analysis.json) |
| Source correspondence | Bundled application modules match the published Python sources byte for byte. [SHA-256 manifest](evidence/app-source-sha256.json) |

## Security and fault coverage

Tests exercise authentication on every control route, minimal public health, hostile Host/Origin values, malformed and oversized JSON, duplicate keys, non-finite numbers, depth/node limits, unsupported schema features, unsafe policy files, private-file permissions, symlinks/hardlinks, database schema attacks, and bounded history. Prompt and generated-response content are excluded from the journal.

Both simulated Ollama and LM Studio peers exercise text, streaming, function tools, capability checks, explicit-model constraints, malformed/oversized/truncated output, first-chunk and overall deadlines, schema post-check failures, disconnected clients, overload, and recovery. Active, resident, external and uncertain allocations are protected. A partial stream is never silently retried through another model.

Approval tests use a local fake provider: exact effective payload/provider/model binding, privacy tags, tampering, expiry, queue bounds, denial, disabled escalation, and eight simultaneous approvals producing one send. No real paid-provider call was made. The native approval workflow was tested through preview and denial; no user credentials were read or published.

## Stress interpretation

The separate gateway process handled four 1,500-attempt bursts at concurrency 1, 8, 32 and 128, followed by a 60-second, eight-client soak. The soak added 29,712 attempts. The run also included 1,000 warm routing probes, 40 deliberate disconnects and a final successful recovery request.

Gateway process peak RSS was **58.31 MiB**, excluding runtime peers and model weights. Peak estimated model reservation was **4.44 GiB** within a 5 GiB admission allowance. No active model was evicted, and active requests returned to zero. Warm route-only p95 was **0.265 ms** in this fixture setup.

Attempts include expected 422/503 admission failures and injected 502/late-stream failures. These numbers are not successful inference throughput, real-model latency under load, GPU peak memory, or a long-duration thermal soak. Production performance depends on runtime, model, context and hardware.

## Real-model scope

The test model was Qwen3 0.6B, imported locally as `switchyard-alpha-test:latest`, approximately 523 MB, served by Ollama 0.35.0. Prompts were synthetic. Cold text completed in 1.816 seconds; the other five requests took 0.666–0.757 seconds. Returned model receipts matched final journal outcomes. The model was unloaded after testing; the existing 27B model was not used.

An isolated test Ollama process could not initialize its Metal backend in the execution environment. The successful run used the existing idle local Ollama service with an explicitly pinned, locally imported model. Its global cloud setting was not changed or certified disabled. These tests are evidence of local inference with that model, not a packet-capture proof of runtime egress isolation. The small model remains installed and unloaded.

## Native checks and release limits

Visual checks used a test copy of the release bundle with a disposable data profile, isolated port, and simulated runtime. Production defaults were not changed in the shipped bundle. The modern design retains native text editing, accessibility controls and keyboard actions. Native import tests use real AppKit/PyObjC, catching the callback-signature problem that prevented the recovered baseline from launching.

The app is **ad-hoc signed, without Developer ID signing or Apple notarization**. No valid signing identity was available. Installation can therefore require explicit macOS approval. The bundle targets macOS 13+, but older systems still need acceptance tests. Live LM Studio inference, real frontier-provider sends, large-model memory/thermal pressure, accessibility assistive-technology acceptance, and hosted GitHub CI remain unverified.

Known compatibility limits include a documented OpenAI/Anthropic subset, deterministic heuristic routing, no automatic conversation affinity, no tool execution, and no automatic cloud fallback. See [API](API.md) and [Security](../SECURITY.md). The dependency advisory result does not cover every native/system component; bundled Python's older SQLite library remains present but its stdlib import entry point is blocked, while the journal uses patched APSW/SQLite.

## Reproduce

Use a disposable profile and synthetic prompts. The default automated suite and stress harness use local fixtures and do not require model downloads or provider credentials.

```sh
python -m unittest discover -v
python scripts/stress_gateway.py --output validation-output/stress
python scripts/audit_dependencies.py
python -m pip install bandit==1.9.4
python -m bandit -r switchyard macos scripts -f json -o validation-output/static-analysis.json
```

For actual-model checks, inspect `python scripts/live_ollama_smoke.py --help` and confirm the chosen runtime/model is local before running. Build and package according to [BUILD.md](BUILD.md). Review every new static-analysis finding; the current triage is specific to this release.
