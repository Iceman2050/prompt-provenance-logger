"""prompt-provenance-logger: answer "which context made the agent do this?"

A ProvenanceLogger wraps an agent step function. The agent is modeled as a
callable taking (messages, tools_available) and returning
(tool_calls, assistant_text). Every step records the full system prompt,
user messages, tool results fed back in, tool calls made (with args) and
timestamps. The logger then builds a tool-call DEPENDENCY GRAPH -- which
tool call's output fed which later call's input -- and can walk back from
any tool call to the exact context entries that influenced it.
"""

import json
import re
import time
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

_REF_RE = re.compile(r"\{\{\s*call_(\d+)\s*\}\}")

_STOPWORDS = frozenset(
    "the a an and or of to in on for with is are was were be been it its "
    "this that these those as at by from you your we our i he she they them "
    "not no do does did will would can could should have has had".split()
)


def _utcnow():
    return datetime.now(timezone.utc).isoformat()


def _significant_tokens(text):
    return {
        t.lower()
        for t in re.findall(r"[A-Za-z0-9_.-]+", str(text))
        if len(t) > 3 and t.lower() not in _STOPWORDS
    }


def _longest_common_substring(a, b, minimum=12):
    """Longest common substring of a and b, or "" if shorter than minimum."""
    a, b = str(a), str(b)
    if len(a) < minimum or len(b) < minimum:
        return ""
    best = ""
    # cheap scan: slide windows of `minimum` from the shorter string
    short, long = (a, b) if len(a) <= len(b) else (b, a)
    for i in range(len(short) - minimum + 1):
        window = short[i : i + minimum]
        if window in long:
            # extend greedily
            j = i + minimum
            while j < len(short) and short[i:j + 1] in long:
                j += 1
            if len(short[i:j]) > len(best):
                best = short[i:j]
    return best


# ---------------------------------------------------------------------------
# Core logger
# ---------------------------------------------------------------------------

class ProvenanceLogger:
    """Wraps an agent step function and records a full provenance trace.

    agent_fn(messages, tools_available) -> (tool_calls, assistant_text)
        tool_calls: list of {"name": str, "args": dict}
    tool_executor(name, args) -> str  (optional; results feed the next step)
    """

    def __init__(self, agent_fn, system_prompt, tool_executor=None):
        self.agent_fn = agent_fn
        self.system_prompt = system_prompt
        self.tool_executor = tool_executor
        self._call_seq = 0

    # -- run ---------------------------------------------------------------
    def run(self, user_messages, tools_available, max_steps=10):
        if isinstance(user_messages, str):
            user_messages = [user_messages]
        trace = {
            "created_at": _utcnow(),
            "system_prompt": self.system_prompt,
            "system_prompt_lines": self.system_prompt.splitlines(),
            "user_messages": list(user_messages),
            "tools_available": list(tools_available),
            "steps": [],
            "tool_calls": [],
            "dependency_edges": [],
        }
        messages = [{"role": "system", "content": self.system_prompt}]
        for m in user_messages:
            messages.append({"role": "user", "content": m})

        for step in range(max_steps):
            step_rec = {
                "step": step,
                "timestamp": _utcnow(),
                "messages_in": [dict(m) for m in messages],
                "tool_calls": [],
                "assistant_text": "",
                "tool_results": [],
            }
            raw_calls, assistant_text = self.agent_fn(messages, tools_available)
            step_rec["assistant_text"] = assistant_text
            tool_results = []
            for rc in raw_calls or []:
                self._call_seq += 1
                call = {
                    "id": f"call_{self._call_seq}",
                    "step": step,
                    "name": rc.get("name", ""),
                    "args": rc.get("args", {}),
                    "timestamp": _utcnow(),
                    "result": None,
                }
                step_rec["tool_calls"].append(
                    {"id": call["id"], "name": call["name"], "args": call["args"]}
                )
                if self.tool_executor is not None:
                    result = self.tool_executor(call["name"], call["args"])
                    call["result"] = result
                    tool_results.append(
                        {
                            "call_id": call["id"],
                            "name": call["name"],
                            "args": call["args"],
                            "result": result,
                        }
                    )
                    messages.append(
                        {
                            "role": "tool",
                            "name": call["name"],
                            "call_id": call["id"],
                            "content": result,
                        }
                    )
                trace["tool_calls"].append(call)
            step_rec["tool_results"] = tool_results
            if assistant_text:
                messages.append({"role": "assistant", "content": assistant_text})
            trace["steps"].append(step_rec)
            if not raw_calls:
                break

        trace["dependency_edges"] = build_dependency_graph(trace["tool_calls"])
        return trace

    # -- persistence ---------------------------------------------------------
    @staticmethod
    def save_trace(trace, path):
        with open(path, "w") as f:
            json.dump(trace, f, indent=2, default=str)
        return path

    @staticmethod
    def load_trace(path):
        with open(path) as f:
            return json.load(f)


# ---------------------------------------------------------------------------
# Dependency graph
# ---------------------------------------------------------------------------

def build_dependency_graph(tool_calls):
    """Edges: which earlier tool call's output fed a later call's input.

    Two mechanisms:
      1. Explicit reference: an arg value contains "{{call_N}}".
      2. String overlap: a substring (>=12 chars) of an earlier call's
         result text appears verbatim in a later call's args.
    """
    edges = []
    by_id = {c["id"]: c for c in tool_calls}
    for later in tool_calls:
        args_text = json.dumps(later.get("args", {}), sort_keys=True, default=str)
        # 1. explicit references
        for m in _REF_RE.finditer(args_text):
            ref_id = f"call_{m.group(1)}"
            if ref_id in by_id and ref_id != later["id"]:
                edges.append(
                    {
                        "from": ref_id,
                        "to": later["id"],
                        "mechanism": "explicit_reference",
                        "evidence": m.group(0),
                    }
                )
        # 2. string overlap with earlier results
        for earlier in tool_calls:
            if earlier["id"] == later["id"]:
                continue
            if earlier.get("step", 0) > later.get("step", 0):
                continue
            result = earlier.get("result")
            if not result:
                continue
            overlap = _longest_common_substring(result, args_text)
            if overlap:
                edges.append(
                    {
                        "from": earlier["id"],
                        "to": later["id"],
                        "mechanism": "string_overlap",
                        "evidence": overlap[:120],
                    }
                )
    # dedupe (from, to, mechanism)
    seen, unique = set(), []
    for e in edges:
        key = (e["from"], e["to"], e["mechanism"])
        if key not in seen:
            seen.add(key)
            unique.append(e)
    return unique


# ---------------------------------------------------------------------------
# Provenance query: which context influenced a tool call?
# ---------------------------------------------------------------------------

def explain_tool_call(trace, call_id):
    """Walk back from a tool call to the exact context entries that
    influenced it. Returns a list of influence records ordered by
    specificity (tool results first, then user messages, then system
    prompt lines).
    """
    calls = {c["id"]: c for c in trace["tool_calls"]}
    if call_id not in calls:
        raise KeyError(f"unknown tool call id: {call_id}")
    call = calls[call_id]
    call_text = call["name"] + " " + json.dumps(call.get("args", {}), default=str)
    call_tokens = _significant_tokens(call_text)
    influences = []

    def _overlap_evidence(entry_tokens):
        shared = sorted(call_tokens & entry_tokens)
        return shared[:8], len(shared)

    # 1. Tool results: graph edges + direct string overlap.
    for edge in trace.get("dependency_edges", []):
        if edge["to"] != call_id:
            continue
        src = calls[edge["from"]]
        influences.append(
            {
                "kind": "tool_result",
                "call_id": src["id"],
                "tool": src["name"],
                "mechanism": edge["mechanism"],
                "evidence": edge["evidence"],
                "snippet": str(src.get("result", ""))[:200],
            }
        )

    # 2. User messages with significant token overlap (>=2 shared tokens).
    for i, msg in enumerate(trace.get("user_messages", [])):
        shared, n = _overlap_evidence(_significant_tokens(msg))
        if n >= 2:
            influences.append(
                {
                    "kind": "user_message",
                    "index": i,
                    "evidence": "shared tokens: " + ", ".join(shared),
                    "snippet": str(msg)[:200],
                }
            )

    # 3. System prompt lines with significant token overlap.
    for i, line in enumerate(trace.get("system_prompt_lines", [])):
        if not line.strip():
            continue
        shared, n = _overlap_evidence(_significant_tokens(line))
        if n >= 2:
            influences.append(
                {
                    "kind": "system_prompt_line",
                    "line": i + 1,
                    "evidence": "shared tokens: " + ", ".join(shared),
                    "snippet": line.strip()[:200],
                }
            )

    # Always note the system prompt as ambient context.
    influences.append(
        {
            "kind": "system_prompt",
            "evidence": "ambient context for every step",
            "snippet": trace.get("system_prompt", "")[:120],
        }
    )
    return influences


# ---------------------------------------------------------------------------
# Trace validation
# ---------------------------------------------------------------------------

_REQUIRED_TOP = {
    "created_at", "system_prompt", "user_messages",
    "tools_available", "steps", "tool_calls", "dependency_edges",
}
_REQUIRED_STEP = {"step", "timestamp", "messages_in", "tool_calls",
                  "assistant_text", "tool_results"}
_REQUIRED_CALL = {"id", "step", "name", "args", "timestamp", "result"}


def validate_trace(trace):
    """Return a list of validation errors; empty list means valid."""
    errors = []
    if not isinstance(trace, dict):
        return ["trace is not a dict"]
    for k in _REQUIRED_TOP:
        if k not in trace:
            errors.append(f"missing top-level key: {k}")
    for i, s in enumerate(trace.get("steps", [])):
        for k in _REQUIRED_STEP:
            if k not in s:
                errors.append(f"step {i}: missing key {k}")
    for c in trace.get("tool_calls", []):
        for k in _REQUIRED_CALL:
            if k not in c:
                errors.append(f"call {c.get('id')}: missing key {k}")
    call_ids = {c["id"] for c in trace.get("tool_calls", [])}
    for e in trace.get("dependency_edges", []):
        if e.get("from") not in call_ids:
            errors.append(f"edge references unknown from: {e.get('from')}")
        if e.get("to") not in call_ids:
            errors.append(f"edge references unknown to: {e.get('to')}")
        if e.get("mechanism") not in {"explicit_reference", "string_overlap"}:
            errors.append(f"edge has unknown mechanism: {e.get('mechanism')}")
    return errors


# ---------------------------------------------------------------------------
# Demo / CLI
# ---------------------------------------------------------------------------

def _print_explanation(trace, call_id):
    print(f"\nProvenance for {call_id}:")
    for inf in explain_tool_call(trace, call_id):
        where = inf.get("call_id") or (
            f"user message #{inf['index']}" if inf["kind"] == "user_message"
            else f"system prompt line {inf.get('line')}" if inf["kind"] == "system_prompt_line"
            else "system prompt (ambient)")
        print(f"  - [{inf['kind']}] {where}")
        print(f"      evidence: {inf['evidence']}")
        print(f"      snippet: {inf['snippet'][:120]}")


if __name__ == "__main__":
    import sys
    sys.path.insert(0, "scenarios")
    from mock_agent import make_benign_agent, make_vulnerable_agent, SYSTEM_PROMPT
    from mock_agent import TOOLS, benign_executor, injection_executor

    which = sys.argv[1] if len(sys.argv) > 1 else "both"
    if which in ("benign", "both"):
        logger = ProvenanceLogger(make_benign_agent(), SYSTEM_PROMPT, benign_executor)
        trace = logger.run("Summarize today's inbox highlights.", TOOLS)
        ProvenanceLogger.save_trace(trace, "trace-benign.json")
        print("benign: steps=%d calls=%d edges=%d -> trace-benign.json"
              % (len(trace["steps"]), len(trace["tool_calls"]),
                 len(trace["dependency_edges"])))
        for c in trace["tool_calls"]:
            _print_explanation(trace, c["id"])
    if which in ("injection", "both"):
        logger = ProvenanceLogger(make_vulnerable_agent(), SYSTEM_PROMPT, injection_executor)
        trace = logger.run("Check my notes and summarize.", TOOLS)
        ProvenanceLogger.save_trace(trace, "trace-injection.json")
        print("\ninjection: steps=%d calls=%d edges=%d -> trace-injection.json"
              % (len(trace["steps"]), len(trace["tool_calls"]),
                 len(trace["dependency_edges"])))
        sensitive = [c for c in trace["tool_calls"] if c["name"] == "send_email"]
        for c in sensitive:
            _print_explanation(trace, c["id"])
