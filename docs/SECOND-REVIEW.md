# Second review — 0.3.0-alpha.2

> Historical document: this project was renamed from Switchyard to Loqalyn in Alpha 3. Original names, commands, measurements and evidence below describe the version reviewed at that time.

Reviewed October 13–14, 2026 on Apple silicon, macOS 27.0.1, Python 3.12.15 and Ollama 0.35.0. This update addresses performance, security, setup friction and model discovery. It remains an alpha; this is an engineering review with automated adversarial tests, not an independent penetration-test certification.

## Findings and changes

| Area | Finding | Change and evidence |
| --- | --- | --- |
| Concurrency | Runtime discovery held the memory ledger lock across network requests, delaying admission and release. | Fetch outside the lock; use per-runtime mutation revisions to discard snapshots made stale by a lease/load/unload. Regression tests prove a lease can release during delayed discovery and its new reservation survives. |
| Routing cost | Protected memory was recomputed for every candidate. | Compute it once. A 512-model, 1,400-selection comparison fell from 3.346 ms to 0.597 ms median per selection, **5.6× faster**. This is selection overhead, not LLM token throughput. [Benchmark](evidence/review-routing-benchmark.json) |
| Discovery cost | Every refresh reread every Ollama model's full metadata serially. | Four bounded metadata requests at a time and an 8 MiB cache keyed by manifest digest. Cloud state and model aliases are still rechecked before use. |
| Runtime changes | Changing a runtime URL could leave old, unloaded model metadata selectable. | Clear that runtime's old inventory and block address changes during downloads. A regression covers the policy-to-routing transition. |
| Privacy | A saved local-only checkbox could become stale after Ollama cloud access was re-enabled. | Check `/api/status` before use and block an explicit enabled setting, including warm models. Unsupported older status endpoints and LM Studio retain manual confirmation. This is not packet-level egress attestation. |
| Stream cleanup | Error/EOF writes could wait indefinitely on a stalled client and hold a lease. | Bound completion/error writes and close the connection on stalled cleanup; verify release and a subsequent successful generation. Send no-store/security headers before streaming starts. |
| Input/output contracts | The text JSON helper counted characters rather than UTF-8 bytes; JSON-object mode accepted other JSON types. | Enforce encoded byte size and require an actual object. Regression cases include multibyte input, arrays, strings, numbers and null. |
| Retention | Age limits were applied at startup, not consistently during long runs or policy updates. | Maintain age retention while running; apply changes immediately. Zero clears the journal and disables new records. |
| Memory estimates | Some architectures report K/V dimensions different from embedding size divided by attention heads. | Prefer explicit attention key/value dimensions, including Qwen3's metadata. Reservations remain estimates, not OS allocation controls. |
| Usability | Model identity, reasoning output and setup readiness were confusing. | Show model family/parameter count and memory exclusion details; show the answer first with optional reasoning; cap stream redraws at ten per second and avoid unchanged table reloads. New profiles enable only Ollama; an Ollama-only setup can be verified in Settings. Existing profiles retain their choices. |

## Hugging Face Discover

The new native Discover page searches public Hugging Face GGUF repositories or inspects a publisher/model ID. It shows the publisher, license, exact file, download size, precision and a rough memory estimate. The user reviews the selection once; Switchyard then downloads and installs it through Ollama, reports progress and refreshes the installed-model library. Model weights are not bundled with Switchyard.

The implementation follows [Hugging Face's documented Ollama integration](https://huggingface.co/docs/hub/en/ollama) and [Hub API](https://huggingface.co/docs/hub/en/api). No repository Python code is downloaded or imported. This alpha supports public, ungated, root-level, single-file GGUF weights with reported SHA-256 metadata. Split models, vision projectors, private/gated repositories, MLX and safetensors are not supported by this browser. Runtime architecture support is confirmed during installation; GGUF format alone does not guarantee compatibility. Publishers' model licenses are separate from Switchyard's MIT license.

Searches are explicit, not background requests, and send only the entered search/repository selection. Previews expire after five minutes and the revision/file is rechecked before installation. The adapter checks the expected transfer digest and size, terminal success and Ollama's content-addressed blob endpoint. The live check caught a real protocol detail: progress may omit a final 100% event, so success must not depend on that sampled counter. Ollama performs file hashing/storage; Switchyard does not independently attest the runtime.

A persisted installation gate blocks both automatic and explicit model use until verification succeeds, including after cancellation or restart. Two active downloads and 32 job records are allowed. Cancellation closes the request; Ollama may retain partial files or finish an operation it already accepted. Runtime progress, time and size have finite limits. See [API details](API.md).

## Verification

- **101 automated tests passed**, including real AppKit bridge imports, both simulated runtimes, HTTP/MCP/authentication, private files, approval boundaries and new targeted regressions. [Test log](evidence/review-tests.txt)
- The final synthetic stress run handled **38,672 attempts** with no unexpected outcomes, zero active-model evictions, **59.23 MiB** gateway peak RSS and a **4.44 GiB** reservation peak inside a 5 GiB allowance. All recovery assertions passed, as recorded in [stress evidence](evidence/review-stress.json). Attempts include deliberate upstream failures and capacity rejections; they are not successful inference throughput. Runtime model weights are excluded from gateway RSS.
- **Six real Qwen3 completions passed**: text, streaming, JSON-object validation and repetitions, with matching model receipts and released leases. The pre-existing loaded model was preserved. [Real-model report](evidence/review-live-ollama.json)
- **Live Hugging Face search → inspection → download → installation → catalogue → inference passed**, using a 105,454,432-byte SmolLM2 135M GGUF. Installation took about 100 seconds in this run. The validation model was unloaded and removed afterward; it was a transport test, not a quality recommendation. Its weak answer is retained transparently in the report. [Integration evidence](evidence/review-hub-live.json)
- A larger 639 MB Qwen download progressed to roughly 30% before the harness's 15-minute cap. That attempt was not a completed install and is not counted as a pass. [Slow-download record](evidence/review-hub-slow-download.json)
- Native UI checks exercised live search, repository selection, file/size/license display, the download review dialog and cancellation of that dialog, then completed a synthetic generation in an isolated profile. The new screens and optional-reasoning control rendered correctly. The desktop automation tool stalled during initial launch; subsequent interactions completed. [UI record](evidence/review-native-ui.json)
- **26 locked Python distributions**, no published PyPI advisories or query errors at check time. This does not cover every bundled native library or OS component. [Advisories](evidence/review-dependency-advisories.json)
- Bandit scanned **4,258 lines** with no scan errors or suppressed checks: **39 findings — 1 high, 6 medium, 32 low**. The extra finding is a constant-table SQL delete for zero-day retention. All findings have the same reviewed dispositions as alpha 1: verified maintainer archives, fixed/locked URLs, constant SQL identifiers and trusted local subprocess boundaries. No confirmed high/medium exploitable issue remained within the documented threat model. [Raw scan](evidence/review-static-analysis.json), [original rule triage](STATIC-ANALYSIS.md)
- The final bundle is rebuilt from locked inputs and independently checked after packaging/extraction, including bundled CLI launch and signatures. [Package receipt](evidence/review-package.json), [source correspondence](evidence/review-app-source-sha256.json), [source scan](evidence/review-source-scan.json)

## Remaining limits

Apple Developer ID signing and notarization are still unavailable; the build is ad-hoc signed. Live LM Studio inference, paid-provider sends, older macOS versions, full assistive-technology acceptance, long hardware/thermal soaks and the historical PRD's v1 gates are not certified. No automatic updater, multi-user security boundary or arbitrary-runtime sandbox is added. Routing uses transparent heuristics and user preferences, not calibrated quality benchmarks. The app cannot make a tiny model answer accurately or guarantee a large model will fit physical RAM. External runtime clients can still change allocations between observations.

The prior alpha-1 app/profile remains usable. To upgrade, quit the old app, keep a private profile backup if desired, and open the new app. Existing settings and installed models remain in their current locations. See [build/install notes](BUILD.md) and [security boundaries](../SECURITY.md).
