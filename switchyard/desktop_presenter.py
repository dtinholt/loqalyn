"""Pure presentation and form logic for the native macOS application.

This module deliberately has no Cocoa imports so its validation and approval
invariants can be checked on every supported build host.
"""
from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import yaml

from .core import validate_catalog_patch, validate_policy
from .security import loads

PAGES = ("Overview", "Models", "Discover", "Playground", "Policies", "Activity", "Escalations", "Settings")
ROUTE_NAMES = {"quick_chat": "Everyday tasks", "code_edit": "Code & development", "long_document": "Long documents", "hard_reasoning": "Deep reasoning"}
RUNTIME_NAMES = {"ollama": "Ollama", "lmstudio": "LM Studio"}


def pretty(value: Any) -> str:
    return json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False)


def number(value: Any, label: str, *, integer: bool = False) -> int | float:
    try:
        text = str(value).strip()
        result = int(text) if integer else float(text)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{label} must be {'an integer' if integer else 'a number'}.") from exc
    return result


def request_payload(prompt: str, model: str, max_tokens: str, tools: bool = False) -> dict:
    if not prompt.strip():
        raise ValueError("Enter a prompt first.")
    limit = number(max_tokens, "Output limit", integer=True)
    if not 1 <= limit <= 131072:
        raise ValueError("Output limit must be between 1 and 131072 tokens.")
    body = {"model": model or "auto", "messages": [{"role": "user", "content": prompt}], "max_tokens": limit}
    if tools:
        body["tools"] = [{"type": "function", "function": {"name": "lookup", "description": "Look up a local record", "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}}}]
    return body


def model_patch(values: dict) -> dict:
    return validate_catalog_patch({
        "name": str(values["name"]).strip(),
        "tier": values["tier"],
        "size_gb": number(values["size_gb"], "Weight size"),
        "context_max": number(values["context_max"], "Maximum context", integer=True),
        "kv_gb_per_1k": number(values["kv_gb_per_1k"], "KV estimate"),
        "tools_verified": bool(values["tools_verified"]),
    })


def settings_policy(base: dict, values: dict) -> dict:
    result = copy.deepcopy(base)
    for key, label in (("budget_gb", "Memory budget"), ("headroom_gb", "Headroom")):
        result[key] = number(values[key], label)
    for key, label in (("default_max_tokens", "Default output limit"), ("log_retention_days", "Retention")):
        result[key] = number(values[key], label, integer=True)
    result["runtime_local_only_confirmed"] = bool(values["runtime_local_only_confirmed"])
    result["escalation_mode"] = values["escalation_mode"]
    result["sensitive_terms"] = [line.strip() for line in str(values["sensitive_terms"]).splitlines() if line.strip()]
    result["runtimes"] = {
        runtime: str(values[runtime]).strip()
        for runtime in RUNTIME_NAMES if values.get(runtime + "_enabled", runtime in base.get("runtimes", {}))
    }
    return validate_policy(result)


def policy_yaml(text: str) -> dict:
    if len(text.encode('utf-8')) > 32768:
        raise ValueError('The policy exceeds the 32 KiB limit.')
    try:
        value = yaml.safe_load(text)
    except (yaml.YAMLError, RecursionError) as exc:
        raise ValueError(f"Could not read policy YAML: {exc}") from exc
    return validate_policy(value)


def filtered_models(state: dict, query: str = "") -> list[dict]:
    words = query.casefold().split()
    return [m for m in state.get("models", []) if all(word in " ".join(str(m.get(key, "")) for key in ("name", "id", "runtime", "handle", "tier")).casefold() for word in words)]


def model_status(model: dict) -> str:
    if not model.get('installation_verified',True): return 'Install incomplete'
    if model.get("state_uncertain"):
        return "Recovery required"
    if model.get("available") is False:
        return "Runtime offline"
    if model.get("active"):
        return f"In use ({model['active']})"
    if model.get("loaded"):
        return "Resident" if model.get("tier") == "resident" else "Ready" if model.get("managed") else "Runtime managed"
    return "On disk"


def model_summary(model: dict) -> str:
    context=f"{model['context_max']:,} tokens" if model.get('context_max') else 'Not reported'
    memory=f"{model.get('memory_gb',0):.2f} GiB reserved" if model.get('loaded') else f"{model.get('size_gb',0):.2f} GiB weights on disk"
    features=', '.join(str(x).replace('_',' ').title() for x in model.get('capabilities',[])) or 'Not reported'
    ownership='Managed by Switchyard' if model.get('managed') else 'Managed by the runtime' if model.get('loaded') else 'Available to load'
    return '\n'.join([model.get('name',model['id']), model_status(model)+'  ·  '+RUNTIME_NAMES.get(model['runtime'],model['runtime']), '',
        'Model family              '+model.get('family','Unknown')+'  ·  '+model.get('parameters','Unknown')+' parameters',
        'Memory estimate     '+memory, 'Maximum context     '+context, 'Capabilities              '+features,
        'Tool use                    '+('Verified by you' if model.get('tools_verified') else 'Not yet verified'),
        'Lifecycle                    '+ownership, '', 'Model ID: '+model['id']])


def load_blocker(model: dict, state: dict) -> str | None:
    if not model.get('installation_verified',True): return 'Retry this model download in Discover to complete verification.'
    if not state.get("policy", {}).get("runtime_local_only_confirmed"):
        return "Confirm local runtime setup in Settings before loading models."
    if model.get("state_uncertain"):
        return "Restart this runtime, then choose Recover runtime."
    if model.get("available") is False:
        return "Start the runtime, then choose Discover."
    if not model.get("context_max") or not model.get("size_gb"):
        return "Configure the model's weight size and maximum context first."
    if model.get("loaded"):
        return "This model is already loaded."
    return None


def unload_blocker(model: dict) -> str | None:
    if not model.get("loaded"):
        return "This model is not loaded."
    if model.get("state_uncertain"):
        return "Restart this runtime, then choose Recover runtime."
    if model.get("active"):
        return "Wait for active requests to finish before unloading."
    if model.get("tier") == "resident":
        return "Change the resident tier in Configure before unloading."
    if not model.get("managed"):
        return "This instance was loaded outside Switchyard. Unload it in its runtime."
    return None


def format_time(timestamp: Any) -> str:
    try:
        return datetime.fromtimestamp(float(timestamp)).astimezone().strftime("%b %d, %H:%M:%S")
    except (ValueError, TypeError, OSError, OverflowError):
        return "Unknown"


def decision_text(decision: dict) -> str:
    route = ROUTE_NAMES.get(decision.get("route"), decision.get("route", "Unknown"))
    lines = [f"{route}  •  {decision.get('status', 'routed')}", "", f"Selected model: {decision.get('model_name') or decision.get('model') or 'No eligible local model'}",
             f"Context reserved: {decision.get('context', 0):,} tokens, including output", f"Routing: {decision.get('duration_ms', 0)} ms  •  {decision.get('classifier', 'deterministic rules')}"]
    if decision.get("model"):
        lines.append("Model was already loaded when selected; admission is rechecked before generation." if decision.get("warm") else "Model required loading when selected.")
    if decision.get("escalation"):
        lines.append("Review flag: " + str(decision["escalation"]).replace("_", " "))
    if decision.get("privacy_tags"):
        lines.append("Sensitive content tags: " + ", ".join(decision["privacy_tags"]))
    lines += ["", "Eligible models"]
    for item in decision.get("candidates", []):
        lines.append(f"  {item['id']}  •  score {item.get('score', '—')}  •  {item.get('memory_gb', '—')} GiB estimated")
    if not decision.get("candidates"):
        lines.append("  None. Reduce context, check runtime state, or configure a capable model.")
    if decision.get("exclusions"):
        lines += ["", "Excluded by constraints"]
        for item in decision["exclusions"]:
            lines.append("  " + item["id"] + ": " + ", ".join(item.get("reasons", [])).replace("_", " "))
            if 'memory_budget' in item.get('reasons',[]) and 'memory_gb' in item:
                lines.append(f"    Needs about {item['memory_gb']:.1f} GiB; {item['available_gb']:.1f} GiB available within your policy.")
    return "\n".join(lines)


def parsed_payload(text: str) -> dict:
    if len(text.encode("utf-8")) > 2 * 1024 * 1024:
        raise ValueError("The review payload exceeds the 2 MiB limit.")
    try:
        value = loads(text)
    except ValueError as exc:
        raise ValueError('Payload must be finite, bounded JSON with unique keys.') from exc
    if not isinstance(value, dict) or not isinstance(value.get("messages"), list) or not value["messages"]:
        raise ValueError("Payload must be an object with a non-empty messages list.")
    return value


def approval_input(request_id: str, provider: str, model: str, text: str) -> dict:
    model = model.strip()
    if provider not in ("openai", "anthropic") or not model or len(model) > 250:
        raise ValueError("Choose a provider and enter its model ID.")
    return {"request_id": request_id, "provider": provider, "model": model, "payload": parsed_payload(text)}


@dataclass(frozen=True)
class ApprovalReview:
    """An approval is usable only for the exact inputs that were previewed."""
    original: dict
    preview: dict

    def approval_body(self, current: dict, confirmed: bool) -> dict:
        if current != self.original:
            raise ValueError("The payload, provider, model, or selection changed. Preview it again before approving.")
        if not confirmed:
            raise ValueError("Review the exact outgoing payload and confirm this one request.")
        if self.preview.get("tags"):
            raise ValueError("Redact detected sensitive content, then preview again: " + ", ".join(self.preview["tags"]))
        if self.preview.get("provider") != current["provider"] or self.preview.get("model") != current["model"] or not self.preview.get("hash"):
            raise ValueError("The service preview did not match the selected provider and model.")
        return {"action": "approve", "confirmed": True, "hash": self.preview["hash"], "payload": copy.deepcopy(current["payload"]), "provider": current["provider"], "model": current["model"]}


class StreamBuffer:
    """Accumulate controller callbacks without executing generated tool calls."""
    def __init__(self):
        self.content = ""
        self.reasoning = ""
        self.tools: dict[int, dict] = {}
        self.finish_reason: str | None = None
        self.model: str | None = None

    def add(self, chunk: str | dict) -> None:
        if isinstance(chunk, str):
            self.content += chunk
            return
        if not isinstance(chunk, dict):
            return
        if chunk.get("error"):
            error = chunk["error"]
            raise ValueError(error.get("message", "Generation failed.") if isinstance(error, dict) else str(error))
        if chunk.get("model"):
            self.model = chunk["model"]
        for choice in chunk.get("choices", []):
            delta = choice.get("delta", {})
            self.content += delta.get("content") or ""
            self.reasoning += delta.get("reasoning_content") or ""
            if choice.get("finish_reason"):
                self.finish_reason = choice["finish_reason"]
            for tool in delta.get("tool_calls", []):
                item = self.tools.setdefault(tool.get("index", 0), {"name": "", "arguments": ""})
                function = tool.get("function", {})
                item["name"] += function.get("name") or ""
                item["arguments"] += function.get("arguments") or ""

    def text(self, show_reasoning=False) -> str:
        parts = []
        if self.content:
            parts.append(self.content)
        if self.tools:
            parts.append("TOOL CALLS (not executed by this playground)\n" + pretty(list(self.tools.values())))
        if show_reasoning and self.reasoning:
            parts.append("MODEL REASONING\n" + self.reasoning)
        return "\n\n".join(parts)
