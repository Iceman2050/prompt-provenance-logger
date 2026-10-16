"""Tests for prompt-provenance-logger. Run: python3 -m unittest discover -s tests"""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scenarios"))

from provenance import (ProvenanceLogger, build_dependency_graph,
                        explain_tool_call, validate_trace)
from mock_agent import (make_benign_agent, make_vulnerable_agent, SYSTEM_PROMPT,
                        TOOLS, benign_executor, injection_executor)


def run_benign():
    logger = ProvenanceLogger(make_benign_agent(), SYSTEM_PROMPT, benign_executor)
    return logger.run("Summarize today's inbox highlights.", TOOLS)


def run_injection():
    logger = ProvenanceLogger(make_vulnerable_agent(), SYSTEM_PROMPT,
                              injection_executor)
    return logger.run("Check my notes and summarize.", TOOLS)


class TestBenignScenario(unittest.TestCase):
    def test_trace_structure(self):
        trace = run_benign()
        self.assertEqual(validate_trace(trace), [])
        self.assertEqual(len(trace["tool_calls"]), 3)
        self.assertEqual([c["name"] for c in trace["tool_calls"]],
                         ["read_note", "get_weather", "summarize"])

    def test_explicit_reference_edge(self):
        trace = run_benign()
        edges = trace["dependency_edges"]
        ref_edges = [e for e in edges if e["mechanism"] == "explicit_reference"]
        self.assertEqual(len(ref_edges), 1)
        self.assertEqual((ref_edges[0]["from"], ref_edges[0]["to"]),
                         ("call_1", "call_3"))

    def test_no_sensitive_call(self):
        trace = run_benign()
        self.assertFalse([c for c in trace["tool_calls"]
                          if c["name"] == "send_email"])

    def test_timestamps_recorded(self):
        trace = run_benign()
        for step in trace["steps"]:
            self.assertIn("timestamp", step)
        for call in trace["tool_calls"]:
            self.assertIn("timestamp", call)


class TestInjectionScenario(unittest.TestCase):
    def test_sensitive_call_happened(self):
        trace = run_injection()
        sensitive = [c for c in trace["tool_calls"] if c["name"] == "send_email"]
        self.assertEqual(len(sensitive), 1)
        self.assertEqual(sensitive[0]["args"]["to"], "attacker@evil.example")

    def test_sensitive_call_attributed_to_injected_tool_result(self):
        trace = run_injection()
        sensitive = [c for c in trace["tool_calls"] if c["name"] == "send_email"][0]
        influences = explain_tool_call(trace, sensitive["id"])
        tool_hits = [i for i in influences if i["kind"] == "tool_result"]
        self.assertTrue(tool_hits, "expected the tool result to be blamed")
        self.assertEqual(tool_hits[0]["call_id"], "call_1")
        # The attacker address must appear verbatim in the blamed result.
        self.assertIn("attacker@evil.example", tool_hits[0]["snippet"])

    def test_user_message_not_blamed_for_injection(self):
        trace = run_injection()
        sensitive = [c for c in trace["tool_calls"] if c["name"] == "send_email"][0]
        influences = explain_tool_call(trace, sensitive["id"])
        user_hits = [i for i in influences if i["kind"] == "user_message"]
        self.assertEqual(user_hits, [],
                         "benign user message must not be blamed")

    def test_string_overlap_edge_exists(self):
        trace = run_injection()
        edges = trace["dependency_edges"]
        overlap = [e for e in edges
                   if e["from"] == "call_1" and e["to"] == "call_2"
                   and e["mechanism"] == "string_overlap"]
        self.assertEqual(len(overlap), 1)
        self.assertIn("attacker@evil.example", overlap[0]["evidence"])


class TestGraphAndQuery(unittest.TestCase):
    def test_graph_edge_correctness_on_synthetic_calls(self):
        calls = [
            {"id": "call_1", "step": 0, "name": "fetch",
             "args": {}, "timestamp": "t", "result": "secret-token-XYZ-12345"},
            {"id": "call_2", "step": 1, "name": "use",
             "args": {"payload": "here is secret-token-XYZ-12345 ok"},
             "timestamp": "t", "result": None},
            {"id": "call_3", "step": 1, "name": "other",
             "args": {"x": "unrelated"}, "timestamp": "t", "result": None},
        ]
        edges = build_dependency_graph(calls)
        self.assertEqual(len(edges), 1)
        self.assertEqual((edges[0]["from"], edges[0]["to"]), ("call_1", "call_2"))

    def test_explain_unknown_call_raises(self):
        trace = run_benign()
        with self.assertRaises(KeyError):
            explain_tool_call(trace, "call_999")

    def test_trace_json_roundtrip_and_validation(self):
        trace = run_injection()
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            path = f.name
        try:
            ProvenanceLogger.save_trace(trace, path)
            with open(path) as f:
                loaded = json.load(f)
            self.assertEqual(validate_trace(loaded), [])
            self.assertEqual(len(loaded["tool_calls"]), len(trace["tool_calls"]))
        finally:
            os.unlink(path)

    def test_validation_catches_bad_trace(self):
        errors = validate_trace({"steps": []})
        self.assertTrue(any("missing top-level key" in e for e in errors))
        bad = run_benign()
        bad["dependency_edges"].append(
            {"from": "call_404", "to": "call_1", "mechanism": "string_overlap",
             "evidence": "x"})
        errors = validate_trace(bad)
        self.assertTrue(any("call_404" in e for e in errors))


if __name__ == "__main__":
    unittest.main(verbosity=2)
