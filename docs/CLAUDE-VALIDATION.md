# Claude Code release validation — 0.4.0-alpha.1

Validated on October 14, 2026, on an Apple silicon Mac with 32 GiB RAM, macOS 27.0.1, bundled Python 3.12.15, Claude Code 2.1.299 and Ollama 0.35.0. This is an alpha acceptance record, not an independent penetration test, broad coding benchmark or certification of every runtime/model combination.

## Automated and protocol checks

| Check | Result | Evidence |
| --- | --- | --- |
| Unit, integration, HTTP and native bridge suite | 137 passing tests | [Test log](evidence/claude-tests.txt) |
| Real Claude Code client against a deterministic runtime | Read → Edit → Bash test run → final answer; four API requests, one task/model; four independent Python tests passed; test files unchanged; no permission denials | [Client fixture report](evidence/claude-client-fixture.json) |
| HTTP task concurrency | 192 requests across 64 task IDs; existing tasks kept their model after a preference change, new tasks selected the new preference | Included in the test suite |
| Separate gateway process stress | 41,424 attempts; no unexpected outcomes; 40 disconnect cases; recovery succeeded; no active model evicted | [Stress report](evidence/claude-stress.json) |
| Dependency advisory check | 26 locked Python distributions; no reported advisories or query errors | [Dependency report](evidence/claude-dependencies.json) |
| Packaging | Clean extraction, bundled CLI launch and signatures passed | [Package receipt](evidence/claude-package.json) |
| Installed/source correspondence | Application modules match the installed bundle byte for byte | [Source hashes](evidence/claude-app-source-sha256.json) |
| Source pattern scan | No matching provider-credential patterns | [Scan scope](evidence/claude-source-scan.json) |

The stress run used simulated runtimes. Warm route-only p95 was 0.259 ms and gateway process peak RSS was 59.70 MiB, excluding models/runtime processes. Peak model reservation was 4.44 GiB within a 5 GiB allowance. Attempts include deliberately injected failures and expected admission/overload rejections; they are not successful real-model throughput. All active leases were released.

New security coverage includes inference-only credential enforcement, missing/malformed task IDs, persistent locks after an HTTP gateway restart, changed manifests/addresses, revoked verification, external memory reservations, capacity exhaustion without lock eviction, explicit context failure, cancellation/failure continuity, cloud exclusion, safe launcher quoting and filtered child-process credentials. Tool tests cover cache hints, actual client schema shapes, external reference rejection, unsupported media/thinking, tool-result mapping, multiple sequential tool blocks, and withholding invalid tool calls.

## Static security review

Bandit reviewed the application source. Its findings were manually inspected: subprocess imports/calls use fixed argument lists and an owned, non-world-writable Claude binary; the empty auth-token value clears inherited cloud authentication; SQL table-name interpolation in the existing journal only receives fixed internal table names while values remain parameterized. Existing empty-token initialization, fixed sysctl invocation and shutdown cleanup findings remain as documented in [the earlier triage](STATIC-ANALYSIS.md). No high-severity finding was reported. A static scan does not establish the absence of vulnerabilities.

## Native app and upgrade

The installed native app opens successfully, detects Claude Code, shows model eligibility reasons, disables launch when no model is ready, navigates to Discover, and accepts a project through the macOS folder picker. The nine-page sidebar and coding setup layout were inspected visually. The existing alpha was retained as a rollback copy. Existing Loqalyn token, policy and catalogue fingerprints matched immediately after installation. The completed tool check adds a backwards-compatible catalogue flag and a separate model-revision record; the coding feature also adds its own credential and task table. The original administrative token, policy and global Claude Code settings remained unchanged. [Installed-app checks](evidence/claude-installed-check.json).

## Real-model acceptance

Claude Code 2.1.299 completed a real local coding task with Qwen3.5 4B (3,324,173,934 bytes downloaded) through Ollama 0.35.0. It read a Python file, repaired the discount calculation, ran the test command, and explained the fix. All four independent tests passed; their files were unchanged. The four API requests stayed on the same model and task lock, with no permission denials. The coding workflow took 16.78 seconds on this machine, after model verification. This small task is not a general coding benchmark. [Real-client report](evidence/claude-client-live.json).

The installed app independently passed its two-request tool check for this model and displayed it as ready. Selecting the demo folder enabled **Open Claude Code**, and macOS accepted the Terminal launch. The computer-control environment prohibits inspecting Terminal, so the interactive Terminal screen was not visually validated; the separate real-client workflow above exercises the actual Claude Code executable. The installed Loqalyn screen and project picker were visually checked.

## Limits

The client test uses a synthetic Python repair and narrowly permitted tools inside a disposable project. It does not establish general repository-scale coding quality. Basic model verification is a two-request function/tool-result check, not a quality score. The source profile uses conservative byte-based context admission, text only, and Ollama only for coding. Supported client settings, transcript retention and network/tool boundaries are documented in [Claude Code setup](CLAUDE-CODE.md).

The app is ad-hoc signed without Apple Developer ID notarization. Older macOS targets, live LM Studio coding, large-model thermal soak, paid cloud providers, OS-wide egress isolation and the historical PRD's full v1 acceptance remain outside this release's validation.
