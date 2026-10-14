# Router research and adopted practices

Research date: October 13, 2026. Architectural inspiration only; no third-party router source was copied, installed, or relicensed into Switchyard.

| Project | Relevant practice | Switchyard alpha implementation |
| --- | --- | --- |
| [GitHub/Microsoft Project HydraFusion](https://github.blog/ai-and-ml/project-hydrafusion-frontier-quality-via-multi-model-orchestration/) | Validate dispatch plans; explicit cancellation and time limits | Capability/admission rechecks, bounded execution, cancellation-safe release; no partial-output replay |
| [llama-swap](https://github.com/mostlygeek/llama-swap) (MIT) | Model lifecycle scheduling and scarce-memory coordination | Protected active/resident/external models, feasibility before eviction, recovery after uncertain state; stress-tested alternating models |
| [LiteLLM](https://docs.litellm.ai/docs/routing) (MIT outside its separately licensed enterprise area) | Bounded admission, timeouts, private observability | Separate inference/control caps, Retry-After, bounded queues, sanitized errors, prompt-free decision records |
| [RouteLLM](https://github.com/lm-sys/RouteLLM) (Apache-2.0) | Representative routing evaluation and calibration | Repeatable fixtures and explicit separation of simulated correctness from actual-model quality |
| [vLLM Semantic Router](https://vllm-sr.ai/docs/faq/) (Apache-2.0 repository) | Inspect selection evidence and model continuity | Requested/served model identity, named score contributions, policy fingerprint, dry-run preview, explicit model pinning guidance |

HydraFusion is a research preview; this research did not verify a public implementation/license. The similarly named AICPS/hydrafusion repository concerns autonomous-driving sensor fusion and is unrelated. Switchyard does not claim affiliation or HydraFusion-equivalent quality.

RouteLLM's documented `mf` and `sw_ranking` paths can require OpenAI embeddings even with local inference. Those defaults were not adopted. Switchyard's alpha classifier stays deterministic and local. We also deferred compound multi-model critique/cascades, learned confidence, automatic health cooldowns, and automatic conversation affinity. They need dedicated evaluation and failure semantics before inclusion.

The [full research record](evidence/router-research.json) contains sources, proposed acceptance checks, and deferred ideas. It describes findings at the start of this work; this page describes the implementation shipped in the alpha.
