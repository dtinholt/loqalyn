# API and compatibility: 0.3 alpha

Send `Authorization: Bearer <local-token>` (or `x-api-key`) on every request except `/health`. Never place tokens in URLs. Local base URL: `http://127.0.0.1:8741`.

| API | Supported contract |
| --- | --- |
| `GET /v1/models` | Virtual routes and discovered runtime-qualified model IDs |
| `POST /v1/chat/completions` | OpenAI-style text/function-tool chat; SSE or collected JSON |
| `POST /v1/messages` | Anthropic-style text/tool subset, including streamed content blocks |
| `POST /loqalyn/route` | Explain selection without loading, inference, production logging, or approval queuing |
| `GET /loqalyn/status` | Runtime/model state, estimates, policy, bounded recent decisions and operational counters |
| `GET /loqalyn/decisions/{id}` | Final outcome for a request, including late streaming errors |
| `POST /loqalyn/refresh` | Refresh both runtime inventories |
| `GET/PUT /loqalyn/policy` | Read/validate/save complete policy |
| `POST /loqalyn/models/load`, `/unload`, `/configure` | Authenticated model control |
| `POST /loqalyn/recover` | Explicit recovery after restarting an uncertain runtime |
| `GET /loqalyn/escalations` and `/{id}` | Bounded temporary review queue; details include retained payload |
| `POST /loqalyn/escalations/{id}` | `preview`, `deny`, or exact-hash `approve`; no automatic approval |
| `POST /loqalyn/pull` | Explicit confirmed runtime model download; job ID returned |
| `GET /loqalyn/jobs/{id}` | Download phase, completed/total bytes, percent and final outcome |
| `POST /loqalyn/jobs/{id}/cancel` | Cancel this gateway's download request; runtime may retain partial files |
| `POST /loqalyn/hub/search` | `{ "query": "Qwen3" }`; at most 20 public GGUF text-model repositories |
| `POST /loqalyn/hub/inspect` | `{ "repository": "publisher/model" }`; five-minute preview, license, revision and up to 64 eligible files |
| `POST /loqalyn/hub/install` | `{ "preview_id": "hf_…", "filename": "model.gguf", "confirmed": true }`; verified selection, Ollama job returned |

This is a documented subset, not drop-in parity with every OpenAI/Anthropic SDK or coding agent. Unsupported request fields fail explicitly. No Responses API, images, audio, embeddings, hosted tools, parallel choices (`n > 1`), forced function selection, tool execution, or strict tool-argument enforcement. JSON output is checked after generation. Schemas accept a bounded subset: basic types, properties, items, required, scalar enum, ranges, and lengths. References, patterns, combinators, and uniqueness constraints are rejected. With streaming, a post-check failure may occur after partial output; clients must handle error events and inspect the final decision.

Function tools require `tools_verified: true` for the selected model. Every prior assistant tool call needs a matching tool result before another generation. Call IDs must be unique and arguments must be finite JSON objects. Clients should execute only tools they independently authorize; the gateway never executes them.

`X-Loqalyn-Request-Id`, route, and served-model headers describe the initial dispatch. The decision journal records final success/failure/cancellation. Rule scores are heuristic, `confidence` is null, and the policy fingerprint supports comparing decisions. No quality score is invented from a successful HTTP response.

## Rename compatibility

The documented control prefix is now `/loqalyn`. Every former `/switchyard/*` control route remains an authenticated alias to the same handler and state, with the same Host/Origin checks and download/cloud approval requirements. Responses include both `X-Loqalyn-*` and legacy `X-Switchyard-*` routing receipt headers. OpenAI/Anthropic paths and port 8741 are unchanged. The Python package is now `loqalyn`; source integrations should update imports.

## Operational limits

- 2 MiB request and accumulated generated output; 256 KiB runtime line; 8 MiB runtime metadata response.
- JSON depth 32 and 32,768 structural nodes; runtime chunks have tighter bounds.
- 32 accepted inference requests, 8 generation slots, one active request per model; 16 concurrent control requests. Excess work returns 503 with Retry-After. These alpha caps are fixed in source.
- Queue, load, and request deadlines are configurable. Client disconnects close upstream work and release reservations. Failed/interrupted loads retain uncertain reservations until explicit recovery.
- Up to 32 review records / 16 MiB original pending payloads, expiring after 30 minutes; up to 2 active downloads and 32 job records. Completed provider responses are separately bounded to 2 MiB each.
- Decision/event history is pruned by age during operation (within 60 seconds) and immediately when retention changes. Zero days clears the journal and disables new records; request-detail lookups then return 404. History is also bounded to about 10,000 rows, and 32 MiB of retained serialized data per table. Periodic trimming allows up to 99 additional records; database allocation is not a strict 32 MiB file-size cap. Recent-decision responses include at most 2 MiB of serialized records.

400/415 indicate malformed/unsupported input; 401 missing/incorrect auth; 403 locality/privacy/approval restrictions; 409 conflicting lifecycle state; 422 no eligible model; 502 invalid upstream behavior or failed output check; 503 unavailable capacity/runtime/configuration. A late stream failure is an error event without a successful completion terminator. No silent retry or splice to another model occurs.

## Runtime envelope

Ollama uses `/api/status`, `HEAD /api/blobs/{digest}`, `/api/tags`, `/api/ps`, `/api/show`, `/api/generate`, `/api/chat`, `/api/pull`. LM Studio uses its native `/api/v1/models` lifecycle API plus `/v1/chat/completions`. LM Studio needs a server supporting those native endpoints; older OpenAI-only servers are insufficient. Local validation separates simulated protocol tests from actual-model checks; see [VALIDATION.md](VALIDATION.md).

MCP protocol initialization reports `2024-11-05` and supports newline-delimited JSON-RPC. Batch requests and advanced MCP features are not supported. The four available tools do not approve cloud sends.

## Model discovery and downloads

Hugging Face searches are explicit user actions; startup and runtime polling do not contact the Hub. Only search terms and repository/file selections leave Loqalyn. Metadata requests use a fixed HTTPS Hugging Face origin, no forwarded credentials/proxies or redirects, four concurrent slots, a 20-second overall deadline, and a 2 MiB response bound. Up to 32 previews expire after five minutes. Files must have a reported size (1 MiB–128 GiB) and SHA-256, a root-level GGUF filename, and no split-weight or projector naming. Private/gated models and MLX/safetensors are unsupported in this browser. The manual runtime-ID download remains available separately.

Installation rechecks the repository revision and exact file, invokes Ollama's official `hf.co/publisher/repository:filename.gguf` integration, checks reported weight digest/size, terminal success and the content-addressed blob endpoint. Ollama performs hashing and storage. Progress is sampled and can omit its final 100% event. The maximum weight plus small metadata allowance is enforced against reported transfer layers. A model remains `installation_verified: false` in the catalogue across interruptions/restarts until verification succeeds; both explicit and automatic loading are blocked. The authenticated catalogue API can override this, like other trusted-operator settings. Runtime architecture compatibility is checked during installation, not guaranteed by GGUF format alone.

Downloads have a two-hour total deadline and a three-minute read-idle timeout. Cancellation closes the request; the runtime may retain partial files or complete an operation already accepted. Failed installations must be retried through Discover. Disk/RAM figures in the UI are planning estimates, not reservations or free-space guarantees. Model licenses belong to their publishers. See [Hugging Face's Ollama integration](https://huggingface.co/docs/hub/en/ollama).

Supported Ollama versions report `cloud.disabled` through `/api/status`; an explicit false blocks loading and generation, including warm models. 404/405 from older versions retains manual local-only confirmation. An unreachable or malformed status check fails closed. LM Studio still requires manual LM Link confirmation. These are configuration checks, not independent egress attestation.
