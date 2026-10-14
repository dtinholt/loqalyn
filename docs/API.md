# API and compatibility: 0.3 alpha

Send `Authorization: Bearer <local-token>` (or `x-api-key`) on every request except `/health`. Never place tokens in URLs. Local base URL: `http://127.0.0.1:8741`.

| API | Supported contract |
| --- | --- |
| `GET /v1/models` | Virtual routes and discovered runtime-qualified model IDs |
| `POST /v1/chat/completions` | OpenAI-style text/function-tool chat; SSE or collected JSON |
| `POST /v1/messages` | Anthropic-style text/tool subset, including streamed content blocks |
| `POST /switchyard/route` | Explain selection without loading, inference, production logging, or approval queuing |
| `GET /switchyard/status` | Runtime/model state, estimates, policy, bounded recent decisions and operational counters |
| `GET /switchyard/decisions/{id}` | Final outcome for a request, including late streaming errors |
| `POST /switchyard/refresh` | Refresh both runtime inventories |
| `GET/PUT /switchyard/policy` | Read/validate/save complete policy |
| `POST /switchyard/models/load`, `/unload`, `/configure` | Authenticated model control |
| `POST /switchyard/recover` | Explicit recovery after restarting an uncertain runtime |
| `GET /switchyard/escalations` and `/{id}` | Bounded temporary review queue; details include retained payload |
| `POST /switchyard/escalations/{id}` | `preview`, `deny`, or exact-hash `approve`; no automatic approval |
| `POST /switchyard/pull` | Explicit confirmed runtime model download; job ID returned |
| `GET /switchyard/jobs/{id}` | Bounded download-job state |

This is a documented subset, not drop-in parity with every OpenAI/Anthropic SDK or coding agent. Unsupported request fields fail explicitly. No Responses API, images, audio, embeddings, hosted tools, parallel choices (`n > 1`), forced function selection, tool execution, or strict tool-argument enforcement. JSON output is checked after generation. Schemas accept a bounded subset: basic types, properties, items, required, scalar enum, ranges, and lengths. References, patterns, combinators, and uniqueness constraints are rejected. With streaming, a post-check failure may occur after partial output; clients must handle error events and inspect the final decision.

Function tools require `tools_verified: true` for the selected model. Every prior assistant tool call needs a matching tool result before another generation. Call IDs must be unique and arguments must be finite JSON objects. Clients should execute only tools they independently authorize; the gateway never executes them.

`X-Switchyard-Request-Id`, route, and served-model headers describe the initial dispatch. The decision journal records final success/failure/cancellation. Rule scores are heuristic, `confidence` is null, and the policy fingerprint supports comparing decisions. No quality score is invented from a successful HTTP response.

## Operational limits

- 2 MiB request and accumulated generated output; 256 KiB runtime line; 8 MiB runtime metadata response.
- JSON depth 32 and 32,768 structural nodes; runtime chunks have tighter bounds.
- 32 accepted inference requests, 8 generation slots, one active request per model; 16 concurrent control requests. Excess work returns 503 with Retry-After. These alpha caps are fixed in source.
- Queue, load, and request deadlines are configurable. Client disconnects close upstream work and release reservations. Failed/interrupted loads retain uncertain reservations until explicit recovery.
- Up to 32 review records / 16 MiB original pending payloads, expiring after 30 minutes; up to 2 active downloads and 32 job records. Completed provider responses are separately bounded to 2 MiB each.
- Decision/event history is pruned by age, about 10,000 rows, and 32 MiB of retained serialized data per table. Periodic trimming allows up to 99 additional records; database allocation is not a strict 32 MiB file-size cap. Recent-decision responses include at most 2 MiB of serialized records.

400/415 indicate malformed/unsupported input; 401 missing/incorrect auth; 403 locality/privacy/approval restrictions; 409 conflicting lifecycle state; 422 no eligible model; 502 invalid upstream behavior or failed output check; 503 unavailable capacity/runtime/configuration. A late stream failure is an error event without a successful completion terminator. No silent retry or splice to another model occurs.

## Runtime envelope

Ollama uses `/api/tags`, `/api/ps`, `/api/show`, `/api/generate`, `/api/chat`, `/api/pull`. LM Studio uses its native `/api/v1/models` lifecycle API plus `/v1/chat/completions`. LM Studio needs a server supporting those native endpoints; older OpenAI-only servers are insufficient. Local validation separates simulated protocol tests from actual-model checks; see [VALIDATION.md](VALIDATION.md).

MCP protocol initialization reports `2024-11-05` and supports newline-delimited JSON-RPC. Batch requests and advanced MCP features are not supported. The four available tools do not approve cloud sends.
