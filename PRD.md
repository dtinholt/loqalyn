> Historical product brief retained for context. For the shipped 0.3 alpha, see README.md and docs/VALIDATION.md. Requirements below are not claims that every v1 gate has passed.

> Historical document: this project was renamed from Switchyard to Loqalyn in Alpha 3. Original names, commands, measurements and evidence below describe the version reviewed at that time.

# Switchyard — local model router

Product requirements, revised October 13, 2026 · Product owner: Dinand Tinholt

Status: **0.1 implementation beta. The full v1 release remains gated on E1–E6.**

## 1. Product decision

Switchyard gives a Mac owner one local endpoint for the models already installed in Ollama and LM Studio. It explains which model it chose, coordinates model loads against one admission budget, and creates a reviewable suggestion when a frontier model may help. The user keeps their existing inference runtimes and model weights.

The full product promise is “stop choosing and loading models by hand.” The first implemented release makes that workflow useful with deterministic rules, a shared model ledger, streaming API adapters, a control room, CLI, MCP tools, and an explicit escalation queue. Learned routing and hardware-calibrated guarantees are separate release milestones; a working interface is not evidence that those milestones have passed.

The target remains a 128 GB Apple silicon Mac running macOS. First run seeds the configurable budget at the smaller of **96 GiB or 75% of detected physical memory**, rounded down, with a 4 GiB minimum. Headroom is the smaller of 4 GiB or one quarter of that budget. A 96 GiB budget with 4 GiB reserved within it yields a 92 GiB admission limit. These are starting configurations, not universal GPU limits or safe allocations proved on every Mac. Switchyard must never change macOS wired-memory limits automatically.

## 2. What the original PRD got right, and what changes

The original identifies the right integration problem: a model selector alone does not know what is installed, what is already warm, or what another runtime is consuming. The catalog, ledger, gateway, and approval gate should remain one product.

| Gap in the original | Improved requirement |
| --- | --- |
| An explicit model “passes through untouched,” while hard constraints must never be violated. | Explicit selection only narrows the candidates. Context, verified tools, memory, locality, and availability remain mandatory. |
| “Answer locally and flag” is undefined when no local model fits. | Return a structured 422 with exclusions and an optional review-queue entry. Never truncate, invent an answer, or choose an ineligible fallback. |
| Two runtime inventories are treated as complete memory accounting. | Separate estimated reservation, observed runtime allocation, and OS pressure. Account for external loads and concurrent requests; state what cannot be observed. |
| Matching a model name or runtime digest appears sufficient for deduplication. | Cross-runtime identity requires a checksum of the actual weight artifact. A tag, manifest digest, quantization label, or similar name is insufficient. |
| Context accounting is unspecified. | Reserve the whole conversation, system messages, tools, template overhead, and requested output. Label the beta byte estimate as conservative and uncalibrated. Reject images until their accounting is verified. |
| A late post-check is expected to change response headers. | Headers describe the initial decision. Late outcomes update a request-ID-addressable decision record after streaming. |
| “100% privacy” depends on entity recognition. | Heuristic detection is a review aid, not proof of absence. No automatic escalation in 0.1; approval, transport controls, and externally observed egress are separate requirements. |
| Hashed prompts are expected to support replay. | Hashes cannot reconstruct requests. Replay a versioned synthetic or explicitly consented corpus; keep production logs free of prompt content. |
| Quality, compatibility, and performance are described without a tested envelope. | Publish an explicit supported API subset, a runtime-version matrix, and unpassed hardware gates. Distinguish runtime-reported speed from end-to-end benchmarks. |
| Full v1 and the first runnable build share one release claim. | Publish a feature/status matrix and treat the first build as a beta until the complete release evidence exists. |

## 3. Users, outcomes, and boundaries

The primary user owns the machine and uses local models for writing, programming, analysis, and private document work. Secondary clients are chat applications, coding agents, scripts, and MCP-enabled tools on that machine.

| User situation | Required result |
| --- | --- |
| A short everyday request | Prefer an eligible small or warm model; explain the choice. |
| A code edit | Apply the code route and user preferences after all hard constraints. |
| A long document | Reserve prompt plus output; reject with actionable exclusions if none fit. |
| A function-tool conversation | Admit only a model the user or evaluation harness has verified for tools. Preserve tool IDs and arguments. |
| Four concurrent clients | Queue safely; reserve before loading; never unload an active stream. |
| A runtime loaded a model outside Switchyard | Count the visible allocation and protect that external instance from automatic eviction. |
| No network connection | Serve installed local models; keep suggestions local and avoid implicit retries to the cloud. |
| A frontier suggestion | Show the complete outgoing payload for review and redaction; send only the individually approved request. |
| A new model | Download only after an explicit command, discover it, and measure before presenting quality claims. |

Non-goals remain training, fine-tuning, quantization, replacing inference runtimes, or pooling machines. Windows/Linux support, LAN hosting, automatic OS-memory tuning, and a production cloud service are outside v1. The hosted web demo is a product demonstration; it is not the local inference service.

## 4. Delivered 0.1 versus the full v1 target

“Implemented” means source is present. It does not imply validation on Apple silicon or a live upstream runtime.

| Capability | Implemented beta | Full v1 / later acceptance |
| --- | --- | --- |
| Browser control room | Inventory, estimated memory, policies, route dry run, local generation, decisions, escalation review, settings | Accessibility and target-Mac UX acceptance |
| Catalog | Ollama and LM Studio LLM discovery; editable YAML overrides; runtime-qualified IDs | Inventory parity, validated provenance, automatic checksum-backed merging, shelf integration |
| Routing | Keyword rules; `auto`, `auto-fast`, `auto-code`; explicit models; explainable exclusions | Locally hosted classifier, calibrated confidence, evaluation-backed quality ladders |
| Admission | Shared lock, per-model queue, load reservation, managed idle eviction, visible external-instance protection, resident reload, uncertain-state recovery | OS-pressure integration and calibrated cross-runtime memory envelope |
| Inference | Text/function-tool subset of OpenAI chat and Anthropic Messages; streaming and non-streaming | Broader client conformance matrix and verified image support |
| Escalation | `flag` / `off`; manual queue; payload review, heuristic sensitive-data block, macOS Keychain provider keys | Session signals, richer checks; `ask` only after defined timeout behavior. `auto` requires a separate product and safety decision. |
| Observability | Local SQLite decision metadata and events; JSON export | Stable schema and an opt-in export policy; no raw prompt logging |
| CLI / MCP | `yard` commands and four MCP stdio tools | SDK/client interoperability tests and richer hardware evaluation runner |
| Native shell | Swift menu-bar source; status, recent decisions, links to the control room | macOS build/signing, launch lifecycle, and on-device acceptance |
| Installation | Source archive, Python installer, optional menu-bar build and per-user login-service scripts | Homebrew packaging, signed distribution, upgrade/rollback workflow |
| Additional integrations | None | Learned classifier, embeddings fallback, automatic runtime failover, model-shelf and llama-swap adapters are not production integrations in 0.1. |

## 5. Request and decision contract

1. Authenticate and validate the request before runtime work.
   Inference and model loading also require explicit local-only runtime confirmation after the user disables Ollama cloud and LM Studio remote/Link behavior. Discovery and route dry runs remain available before this confirmation.
2. Parse the complete conversation and requested output limit. Reject unsupported content explicitly.
3. Refresh runtime state when stale. Extract route features and privacy tags locally.
4. Exclude unavailable, context-ineligible, memory-ineligible, or tool-unverified models. Explicit model IDs do not bypass this step.
5. Rank survivors using route preference, known task scores, warm state, and estimated cost. The beta uses deterministic keyword rules and reports `confidence: null`.
6. Acquire the per-model queue slot, recheck admission under the ledger lock, reserve memory, and perform any authorized evictions and load.
7. Stream the chosen runtime's output. An active reservation lasts until upstream generation is closed and the request is released.
8. Run supported post-checks, update the decision, and queue a suggestion if applicable.

Every decision has a request ID, time, policy route, chosen runtime/model or no selection, candidates, exclusions, context estimate, warm/cold state, escalation reason, and completion status. Logs must not include messages, tool definitions, tool outputs, generated text, secrets, or reversible prompt encodings.

| Condition | API behavior |
| --- | --- |
| No eligible model | 422, `context_exceeds_local` or `no_eligible_model`, with the decision and exclusions |
| Local image input / remote media URL | 422 `vision_not_verified` for local image data; remote media is rejected before any fetch |
| Unsupported feature / invalid request | 4xx with an explicit reason; no silent semantic substitution |
| Local-only runtime setup unconfirmed | 403 `runtime_locality_unconfirmed` for generation/loading; discovery and dry run remain available |
| Invalid policy or catalog edit | 503 `config_invalid`; repair the file before new routing/admission |
| Queue or admission capacity unavailable | 503 with `Retry-After` |
| Runtime state uncertain after interrupted load/unload | Block admission until the affected runtime is restarted and explicitly recovered |
| Runtime failure before headers | Structured failure; no fabricated completion |
| Runtime failure after tokens | Stream error and failed decision; never replay partial generation automatically |
| Client disconnect | Close upstream work and release the ledger reservation |

Dry run (`POST /switchyard/route`) does not load, generate, persist a production decision, or create an approval request. It explains the decision against the current discovered catalog.

## 6. Catalog and evidence model

Runtime-qualified model IDs, such as `ollama/<handle>` and `lmstudio/<key>`, are stable routing handles within a catalog. Discovery is authoritative about presence; user overrides are authoritative about configured preferences and verified facts, but may not create a nonexistent runtime model.

Required fields include runtime, handle, size, maximum context, format/quantization when known, loaded instances, ownership, estimated/observed allocation, tools verification, tier, and optional task/speed evidence. Unknown values remain unknown. Runtime-advertised tool capability is not proof of correct tool use.

For v1, every measured field must carry runtime version, hardware, model identity, context size, measurement method, and timestamp. User-entered task scores must be clearly labeled as overrides. Only a validated evaluation result may be described as calibrated.

**Identity.** A SHA-256 override may identify a single actual weight file only when the checksum was computed from that file. Multi-file models require a canonical, verified artifact-manifest scheme before automatic equivalence can be claimed. The beta does not treat Ollama's model-manifest digest as a cross-runtime weight checksum and does not automatically merge model entries. A known matching weight checksum can protect against duplicate loading, but missing checksums cannot prove uniqueness.

**Speed.** Ollama's completed response can report output count and decode duration; the beta derives per-response decode tokens/second from those fields. `yard bench` reports end-to-end output tokens/second including loading, queueing, and prefill. Neither is a task-quality score or a calibrated hardware profile. LM Studio speed remains unknown unless measured or explicitly provided.

**Storage.** `policy.yaml` holds routing and admission settings; `catalog.yaml` holds user overrides. Policy and catalog edits are validated and written atomically. Invalid edits preserve the last usable values for display and repair, surface a visible error, and block new routing/admission until corrected. Full v1 also requires provenance for every measured override.

## 7. Context and memory admission

The beta estimates input tokens from the UTF-8 byte length of serialized messages, tools, and system content, plus message/template reserves. It adds the requested output limit and rounds up to a 1,024-token context bucket. This deliberately favors rejection over undercounting for ordinary text, but it is **not model-tokenizer calibration or a universal mathematical guarantee**. Full v1 requires tokenizer/template validation for each supported model family and separate verified image accounting.

The initial footprint estimate includes weight size, a weight overhead multiplier, context-dependent KV cache, a fixed runtime reserve, and additional headroom. Ollama architecture metadata improves KV estimates; unknown architecture uses a conservative fallback. The UI must call these estimates. Estimated allocations are not actual process RSS, GPU allocation, or OS peak memory.

The full ledger must maintain these invariants:

- Admission is atomic across runtimes: observed allocations plus outstanding reservations plus the requested footprint must fit the configured limit.
- Each model has bounded concurrency. Beta requests serialize per model; different models may generate concurrently once admitted.
- Active streams, resident loaded models, and externally managed instances are protected from automatic eviction.
- Idle managed victims are chosen using recency with reload cost; cancel/failure must release locks and reconcile uncertain runtime state.
- Reusing a loaded model must confirm its usable context. Increasing context on an external instance requires explicit runtime action.
- Duplicate actual weights must not be loaded into two runtimes when identity is known and an existing copy cannot safely be released.
- A failed unload does not erase the allocation. A timed-out load is not proof that upstream loading stopped.

Runtime polling cannot exclude a race where another application loads memory between polls, nor can it account for all non-model GPU processes. The beta discovers LLMs/completion models and may not see embeddings, unrelated runtime work, or OS memory pressure. Its ledger is an admission safeguard within that visibility, not an OS-wide no-swap guarantee. E1/E2 must establish a supported envelope on the target Mac. Avoid separate direct-runtime clients when measuring that envelope.

The `resident` tier protects a loaded model and asks the periodic service loop to reload it when absent, after local-only runtime confirmation and only within the budget. An unavailable runtime or invalid configuration can prevent activation. An interrupted runtime operation leaves memory reserved and state uncertain; ordinary admission remains blocked until the user restarts that runtime and explicitly invokes recovery. Recovery refreshes inventory rather than assuming the old reservation can be discarded.

## 8. Runtime adapters and compatibility

| Operation | Ollama implementation | LM Studio implementation |
| --- | --- | --- |
| Discover | `/api/tags`, `/api/show`, `/api/ps` | `/api/v1/models` |
| Load | `/api/generate` with explicit context and `keep_alive: -1` | `/api/v1/models/load` |
| Unload | `/api/generate` with `keep_alive: 0` | `/api/v1/models/unload` by instance ID |
| Generate | Native `/api/chat`, normalized into the gateway's response format | `/v1/chat/completions` |
| Download | Explicit `/api/pull` | Explicit `/api/v1/models/download`; runtime job may continue asynchronously |

The LM Studio adapter targets the native v1 management API documented for LM Studio 0.4.0 and later. This is an API-family requirement, not a tested version pin. There is no beta `lms` fallback. Before v1 release, pin and record exact tested Ollama and LM Studio builds.

The gateway offers a documented subset, not universal OpenAI or Anthropic compatibility. Supported scope is text messages, ordinary function tools with automatic or disabled tool choice, output limits, and the implemented generation options. OpenAI JSON object/schema requests have a local post-check; this does not make every model a strict-schema generator. Tool argument `strict: true` and external schema references are rejected. Anthropic text, tool-use, and tool-result blocks translate through the same local path. Images, hosted tools, extended thinking controls, arbitrary beta features, Responses API, batches, embeddings, and multiple completion choices are outside the current contract. Compatibility must be proven with contract fixtures and actual client traces before naming a client supported.

**Streaming metadata.** `X-Switchyard-Request-Id`, `-Model`, `-Route`, `-Runtime`, `-Warming`, and `-Escalate` report information known when headers are sent. Standard token deltas remain standard token deltas. Headers cannot be changed after a late JSON/schema failure. A client that needs final status must look up `/switchyard/decisions/{request_id}` after stream termination; the control room refreshes that record. Non-streaming responses can include the completed post-check outcome in headers. Warming is a decision attribute, not a promise of immediate headers or a heartbeat during a cold load.

## 9. Escalation and privacy

The beta supports two modes:

| Mode | Behavior |
| --- | --- |
| `flag` | Serve locally when possible and create a manual review item for a suggestion. No-fit requests return 422 and may also create a review item. |
| `off` | Do not create review items. Invalid local requests still fail safely. |

Implemented triggers are policy-marked routes, no eligible local model, and JSON/schema post-check failure. Learned low-confidence detection, repeated tool failure, user retry signals, code parsing, and agent-test result ingestion remain full-v1 work.

Approval follows an explicit sequence: retrieve the complete payload; review and edit it; select provider/model; preview the normalized outgoing provider payload; obtain its hash and detected tags; approve the matching provider, model, and effective payload with affirmative confirmation. A tagged payload is blocked until edited. The beta scanner detects selected patterns, configured sensitive terms, and an explicit private marker. It does not prove that untagged text is public, perform general entity recognition, or infer a source file's privacy from its folder.

The payload review covers system instructions, all messages, and tool definitions. The approval hash binds the provider, model, and effective provider payload. The pending record expires after 30 minutes and resolves only once. Full v1 must prove these properties through the E4 concurrency and tamper tests. No implicit provider retry is allowed: a failed connection may already have incurred a charge or transmitted data.

Provider keys are stored in the macOS Keychain. The beta supports individually approved OpenAI and Anthropic requests; Anthropic escalation does not support arbitrary tool-history conversion. A frontier response is returned to the approval action and displayed in the review UI. It does not replace the already completed local response or resume the original client stream. That distinction resolves the original PRD's ambiguous “same endpoint” promise.

Pending payloads and approved results live in process memory and disappear on restart; production decision metadata is stored separately. Pending records expire after 30 minutes, checked on access as well as periodic cleanup. The queue holds at most 32 records and 16 MiB of retained request payloads, with a 2 MiB per-request limit; old non-sending entries can be dropped to make room. Larger requests may still receive a flag without a retained approval payload. Denial removes the pending payload. Serialized generation chunks retained for completion/post-checks are capped at 8 MiB per response. A durable queue would require a separately designed encrypted retention policy. Hashes and metadata are never a substitute for consent to retain raw prompts.

**Offline boundary.** Loopback runtime URLs, no redirects, and no implicit provider calls restrict Switchyard's own network behavior. A local runtime can itself proxy a cloud or remote model. Disable Ollama cloud features and LM Studio remote/Link behavior before confirming `runtime_local_only_confirmed` in Settings; this confirmation is required for inference, manual loads, and resident activation. Discovery and route dry runs remain available. Explicit cloud/remote model markers remain blocked even after confirmation. Confirmation is an operator assertion, not network attestation: validate the entire process tree with independent egress observation. Installs, dependency installation, model downloads, and explicitly approved frontier requests are network operations and must be described as such.

## 10. Interfaces and operations

- **HTTP:** authenticated local inference, models, route dry run, catalog/status, policy, model controls, decisions, approval queue, and download jobs. Bind to `127.0.0.1`; there is no supported LAN mode in 0.1.
- **Control room:** live local service state when served by `yard serve`; clearly labeled browser-only sample state when hosted as a demo. Hosted demo values must not appear as measurements from the user's Mac.
- **CLI:** `serve`, `status`, `catalog`, `refresh`, `route`, `load`, `unload`, `recover`, `pull`, `bench`, `eval`, `key`, `token`, and `mcp`. Global options precede the subcommand.
- **MCP:** stdio `list_models`, `route`, `load`, and `escalation_status`. No tool grants escalation approval.
- **Menu bar:** optional Swift status shell with 20 recent decisions and a link to review approvals in the control room. The service remains a separate process; an optional per-user LaunchAgent starts it at login.
- **Diagnostics:** redact runtime failure details; retain metadata with configurable age limits; no telemetry by default. The beta prunes persisted records at service startup.

The local token is required even on loopback for control and inference endpoints. Browser origin/host checks and a same-origin policy reduce browser-origin abuse. A process running as the same local user is not a separate trust boundary. Multi-user isolation and remotely exposed services require a different security design.

## 11. Performance and success measures

These are release targets, not achieved beta results:

| Measure | Full v1 target and definition |
| --- | --- |
| Constraint safety | Zero invalid-model selections in the labeled/adversarial corpus and concurrency suite |
| Routing quality | At least 85% of E3 requests choose an oracle-acceptable eligible model |
| Warm service | At least 70% of completed requests need no cold load, measured on a declared representative workload |
| Router latency | P95 at most 250 ms on 1,000 warm decisions; report rule/classifier time separately from queue/load/TTFT |
| Escalation quality | Precision at least 0.8; recall at least 0.9 on the labeled corpus; no claim of universal detection |
| Authorization | Zero unapproved outbound inference requests in independent egress traces |
| Resource use | Gateway process under 500 MB at the declared concurrency/output envelope, excluding runtime processes; report measured peak |
| Preference fit | Overrides below 10% after two weeks, measured only with user consent and local metadata |

## 12. Release gates E1–E6

The validation record must state hardware, OS, runtime/model versions, policy/catalog revisions, fixtures, commands, and evidence files. A missing or unrun gate is not green.

| Gate | Procedure | Required evidence and pass criterion |
| --- | --- | --- |
| E1 — Catalog and fit | Add/remove models in both runtimes; compare inventories. Load verified models at 8k/32k and supported larger contexts. | 100% inventory parity in the supported scope; calibrated estimates within ±10% of measured peaks or a documented conservative envelope with no underestimate; missing metadata blocks admission. |
| E2 — Activation | Replay 500 requests over six installed models from four clients. Include direct-runtime external loads, simultaneous cold loads, context expansion, cancellation, failed unload, timed-out load, and duplicate weights. Sample OS pressure/swap every second. | Zero admitted reservations over limit, zero active-stream eviction, no deadlocks, reservations recovered after faults; no swap growth within the published supported envelope. |
| E3 — Routing | Use 300 synthetic labeled prompts: 75 for each of the four initial routes, with multilingual/adversarial constraints. Score every eligible candidate blind; acceptable set defined before router scoring. | At least 85% acceptable top-1; zero hard-constraint violations; report warm/smallest-acceptable efficiency separately. Unknown confidence is not reported as calibrated. |
| E4 — Escalation and egress | 120 should-suggest, 120 should-stay, and 60 synthetic sensitive cases. Test approve, deny, edit, mismatch, expiry, failure, and supported modes. Observe Switchyard and runtime egress externally. | Precision ≥0.8, recall ≥0.9; zero unapproved sends and zero tagged-payload sends; no silent retry. “Zero” applies to the test corpus, not every possible secret. |
| E5 — Resilience and APIs | Kill runtimes before/after first token; corrupt policy; disconnect clients; exercise text/tool SSE and non-streaming contracts; run 1,000 warm latency probes. | All faults produce declared outcomes, partial generations never replay, invalid configuration blocks admission while preserving last valid values for repair, compatibility fixtures pass; warm routing P95 ≤250 ms. Future failover applies only before output and only to proven equivalent eligible weights. |
| E6 — Regression | Replay at least 1,000 versioned synthetic or consented requests after code/policy/catalog changes; compare decisions with the approved baseline. | Route shifts above 5% receive a written explanation; zero new safety regressions. Production prompt hashes cannot serve as replay inputs. |

`yard eval` in 0.1 checks a small YAML set through the route dry-run endpoint. It is not the live hardware runner for all six suites. `docs/VALIDATION.md` records the evidence actually collected.

## 13. Delivery milestones and decisions still open

1. **Implemented beta:** source distribution, control room, deterministic routing, adapters, admission, local APIs, manual approval path, CLI/MCP, menu-bar source, and synthetic automated checks.
2. **Target-Mac beta acceptance:** verify installed models and native UI; collect E1/E2/E5 hardware and compatibility evidence; fix gaps before making memory or client-compatibility claims.
3. **Routing quality:** curate the E3 corpus; evaluate local classifier candidates and their licenses; implement optional local classification and a measured fallback without outbound inference.
4. **Full v1 hardening:** complete E4/E6, validate resident-set and uncertain-state recovery, add provenance, OS-pressure controls, packaging, and the tested runtime matrix. Complete supported E1–E6 and publish evidence.
5. **Extensions:** model-shelf, llama-swap, richer session signals, verified vision, and broader compatibility. Automatic frontier routing remains a separate decision.

Open decisions: which exact runtime versions to support; which installed models define the oracle; which tokenizer adapters are sufficient; how multi-file weights establish identity; how much memory to reserve on the user's actual machine; whether full v1 requires classifier routing or can meet E3 through rules; and whether `ask` or `auto` escalation offers enough benefit to justify its expanded contract.

## 14. Primary implementation references

Runtime API references were checked October 13, 2026. They specify API behavior; they do not prove this application was tested against live runtimes.

- [Ollama chat API](https://docs.ollama.com/api/chat)
- [Ollama running models API](https://docs.ollama.com/api/ps)
- [Ollama Anthropic compatibility](https://docs.ollama.com/api/anthropic-compatibility)
- [Ollama FAQ, including disabling cloud features](https://docs.ollama.com/faq)
- [LM Studio native REST API](https://lmstudio.ai/docs/developer/rest)
- [LM Studio model list](https://lmstudio.ai/docs/developer/rest/list)
- [LM Studio load](https://lmstudio.ai/docs/developer/rest/load)
- [LM Studio unload](https://lmstudio.ai/docs/developer/rest/unload)
- [LM Studio Anthropic Messages compatibility](https://lmstudio.ai/docs/developer/anthropic-compat/messages)

The original PRD's model-shelf, inference-calculator, llama-throughput-lab, Arch-Router, Plano, Semantic Router, and RouteLLM references remain useful evaluation candidates. Their availability, licenses, behavior, and integration suitability require verification before adoption; none is represented here as an installed production dependency.
