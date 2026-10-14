# Claude Code with automatic local models

Loqalyn 0.4.0-alpha.1 adds a dedicated Claude Code connection. The coding client reads and edits your project, Loqalyn selects and locks a local model, and Ollama runs inference. Claude Code's normal permission prompts remain in place. Loqalyn does not execute model tools itself.

## Start coding

1. Install Claude Code from [Anthropic's official setup instructions](https://code.claude.com/docs/en/setup). This integration requires 2.1.299 or later and was tested with 2.1.299. Install it in `~/.local/bin`, `/opt/homebrew/bin`, or `/usr/local/bin`.
2. Start Ollama, disable its cloud features, and complete **Settings → Verify local setup** in Loqalyn.
3. Install a local model that advertises tool support and at least 65,536 context tokens. It must fit Loqalyn's memory budget at that context size. A model card advertising tools is not sufficient verification.
4. In **Claude Code**, select the model and click **Check tool support**. The check asks for a harmless synthetic function call, supplies a random tool result, and checks that the model reads it. It never invokes shell commands or modifies your project.
5. Choose a project folder and click **Open Claude Code**. A Terminal session opens. Keep Loqalyn and Ollama running while you work.

The same connection is available from the bundled CLI:

```sh
~/Applications/Loqalyn.app/Contents/MacOS/Loqalyn --cli claude /path/to/project
```

From an installed source checkout, use `loqalyn --data-dir /path/to/your/profile claude /path/to/project` with its gateway already running. The source CLI's default profile differs from a new native app profile; use the same directory and port as the gateway.

## Model selection and continuity

- Initial selection applies model readiness, verified tools, context, memory reservations, route preferences, and the router's existing quality/warmth/load-cost heuristics. Architecture and design-review prompts can use the reasoning route; coding uses the code route; auxiliary requests can favor the fast route. This is explainable heuristic selection, not a guarantee of the best answer.
- The client sends opaque session, agent, and prompt identifiers. Loqalyn persists a hashed task identifier, model ID, manifest revision, runtime address, request count and timestamps. Prompt text, source files, tool arguments, and answers are excluded from this task database.
- A new prompt identifier permits a new selection. Tool calls and retries within that task keep their model. Child agents have their own task identities. Compaction without a prompt hint uses that agent's latest task lock; older clients without prompt hints remain pinned for the entire session.
- Model disappearance, changed weights, changed runtime address, removed verification, or insufficient memory produces an explicit error. Loqalyn does not silently fall back, replay partial tool output, or queue cloud escalation for a coding task.
- Locks survive restarts. They are bounded to 16,384 records and are not silently evicted. **Reset coding connection** revokes the old coding credential and clears locks; it requires idle generation. Close old Claude Code sessions and launch new ones afterward.

The **Recent tasks** table shows model locks and request counts. Activity shows individual request outcomes. Tool verification is tied to Ollama's manifest revision, so updating a model requires a new check.

## Privacy and permissions

The launcher creates an isolated `claude-code` profile under Loqalyn's existing data folder. It does not modify `~/.claude/settings.json`. It disables nonessential client traffic, prompt caching and experimental request extensions, and starts Claude Code in its minimal mode without importing your hooks, plugins or external MCP configuration. Its tool set is Read, Edit, Write, Bash, Glob and Grep. Existing project instructions should be read as part of the task; minimal mode does not preload CLAUDE.md files.

Claude Code can save its own conversations and project excerpts in that separate local profile. Loqalyn's decision-log retention setting does not delete Claude Code's transcripts. Review those files before sharing a profile or support bundle.

Only the separate `claude-token` credential is passed to the client. It can call Messages, token estimation, model discovery and the connection probe. It cannot read Loqalyn's administrative status, change policies, download models, inspect escalation payloads, or approve cloud requests. The launcher file contains paths, not credentials.

Model inference is local. This is not an OS-level network sandbox for Claude Code, a protection against malicious software running as your macOS user, or a guarantee that approved Bash commands cannot use the network. Review the commands and file changes Claude Code proposes. Do not turn off its permission prompts to compensate for a weak model.

## Current limits

- Ollama text coding only. LM Studio remains supported by the existing chat API, but is not eligible for this coding profile yet.
- Local models need reliable tool training. Passing the two-request tool check establishes basic compatibility, not broad coding quality. A larger or better coding model may be necessary for your project.
- The profile disables extended thinking. Advisory effort and cache hints are accepted without promising cloud-style reasoning budgets or prompt caching. Images, PDFs, hosted tools, forced tool choices and unsupported Anthropic extensions fail explicitly.
- Tasks reserve at least 64K runtime context. Input admission uses the existing conservative UTF-8 byte estimate plus output budget, not the model's exact tokenizer. Large requests can therefore be rejected before the model's advertised token limit; compact or divide the task. `/v1/messages/count_tokens` returns this conservative estimate and marks it in `X-Loqalyn-Token-Estimate`.
- These limits are validated for Claude Code 2.1.299. Future client versions may introduce fields that require a compatibility update. Local-model connections are outside Anthropic's Claude-model support scope.
- Loqalyn remains an ad-hoc signed alpha without Apple notarization.
- Claude Code may display a nominal cost estimate for an unknown model alias. That display is not a cloud invoice; Loqalyn's coding connection sends inference only to the local runtime.

## Validation

Run the unit and HTTP tests:

```sh
python -m unittest discover -v
python scripts/claude_code_smoke.py --mode fixture --output validation-output/claude-fixture.json
```

The latter invokes the actual installed Claude Code binary against a deterministic local runtime. It grants only Read, Edit, and one exact test-runner command inside a disposable project. It checks the independent test result, unchanged tests, changed application code, tool permissions, and model continuity.

To test a downloaded model, explicitly select it:

```sh
python scripts/claude_code_smoke.py --mode live --model qwen3.5:4b --output validation-output/claude-live.json
```

That command runs real inference and consumes local RAM. The harness uses a disposable gateway profile, preserves existing external model reservations, and unloads only a model it loaded itself. Its synthetic coding task is not a general quality benchmark.

Protocol references: [Claude Code gateway protocol](https://code.claude.com/docs/en/llm-gateway-protocol), [gateway connection settings](https://code.claude.com/docs/en/llm-gateway-connect), [model configuration](https://code.claude.com/docs/en/model-config), and [Ollama's Claude Code integration](https://docs.ollama.com/integrations/claude-code).
