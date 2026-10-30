"""Offline regression checks for the role manuals sent to the model provider."""
import copy
import json
import re
import unittest
from pathlib import Path

from prism_runtime.config import LAYER_ORDER, default_config
from prism_runtime.domain import roster
from prism_runtime.provider import ModelProvider, ProviderError, output_schema


_EMPTY_OUTPUTS = {
    "collector": {"summary": "No supported findings.", "findings": []},
    "analyst": {"summary": "No supported hypotheses.", "edges": []},
    "strategist": {"summary": "No eligible candidates.", "strategies": []},
    "executor": {"summary": "No justified advisory action.", "decisions": []},
}
_FAKE_KEY = "offline-test-key-must-never-enter-a-prompt"


def _one_agent_per_layer():
    config = default_config()
    config["layers"] = {
        layer: {**settings, "count": 1}
        for layer, settings in config["layers"].items()
    }
    return roster(config)


def _completed_response(value, model):
    return {
        "status": "completed",
        "model": model,
        "usage": {"input_tokens": 1, "output_tokens": 1},
        "output": [{"type": "message", "content": [{
            "type": "output_text", "text": json.dumps(value),
        }]}],
    }


def _assert_strict_objects(test, schema):
    """Check the actual Responses schema has no open object at any depth."""
    schema_type = schema["type"]
    if schema_type == "object":
        properties = schema["properties"]
        test.assertIs(schema.get("additionalProperties"), False)
        test.assertEqual(set(schema["required"]), set(properties))
        for child in properties.values():
            _assert_strict_objects(test, child)
    elif schema_type == "array":
        test.assertIn("maxItems", schema)
        _assert_strict_objects(test, schema["items"])


def _json_payload_of_size(size):
    """Build compact UTF-8 JSON of an exact byte size, including a multibyte char."""
    payload = {"boundary": ""}
    encoded_empty = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    emoji_bytes = len("🙂".encode("utf-8"))
    filler_size = size - len(encoded_empty.encode("utf-8")) - emoji_bytes
    if filler_size < 0:
        raise ValueError("Requested payload is too small for its multibyte marker")
    payload["boundary"] = "x" * filler_size + "🙂"
    return payload


class AgentManualTests(unittest.TestCase):
    def test_roster_loads_all_manuals_within_the_provider_procedure_limit(self):
        agents = _one_agent_per_layer()
        manuals = {agent["layer"]: agent["instructions"] for agent in agents}

        self.assertEqual(set(manuals), set(LAYER_ORDER))
        for layer, manual in manuals.items():
            with self.subTest(layer=layer):
                self.assertIsInstance(manual, str)
                self.assertTrue(manual.strip())
                manual_path = Path(__file__).resolve().parents[1] / "agent_roles" / f"{layer}.md"
                self.assertEqual(manual, manual_path.read_text())
                self.assertLessEqual(len(manual), 6000)

    def test_actual_requests_carry_manuals_and_strict_role_schemas(self):
        agents = _one_agent_per_layer()
        requests = {}

        for agent in agents:
            layer = agent["layer"]

            def transport(body, key, *, role=layer):
                requests[role] = (copy.deepcopy(body), key)
                return _completed_response(_EMPTY_OUTPUTS[role], body["model"])

            result = ModelProvider(lambda: _FAKE_KEY, transport).run(agent, {"fixture": "offline"})
            self.assertIn("output", result)

        self.assertEqual(set(requests), set(LAYER_ORDER))
        for agent in agents:
            layer = agent["layer"]
            body, transport_key = requests[layer]
            with self.subTest(layer=layer):
                developer_prompt = body["input"][0]["content"]
                # The full roster-loaded procedure, including its factual source,
                # mechanism, and invalidation requirements, reaches the request.
                self.assertTrue(developer_prompt.endswith("Task procedure:\n" + agent["instructions"]))
                self.assertEqual(transport_key, _FAKE_KEY)
                self.assertNotIn(_FAKE_KEY, json.dumps(body, ensure_ascii=False))
                self.assertNotIn("tools", body)

                response_format = body["text"]["format"]
                self.assertEqual(response_format["type"], "json_schema")
                self.assertIs(response_format["strict"], True)
                self.assertEqual(response_format["schema"], output_schema(layer))
                _assert_strict_objects(self, response_format["schema"])

    def test_executor_alias_uses_execution_contract_with_executor_manual(self):
        agent = next(agent for agent in _one_agent_per_layer() if agent["layer"] == "executor")
        captured = {}

        def transport(body, key):
            captured["body"] = body
            captured["key"] = key
            return _completed_response(_EMPTY_OUTPUTS["executor"], body["model"])

        ModelProvider(lambda: _FAKE_KEY, transport).run(agent, {})
        body = captured["body"]

        self.assertEqual(output_schema("executor"), output_schema("execution"))
        self.assertEqual(body["text"]["format"]["name"], "prism_execution")
        self.assertEqual(body["text"]["format"]["schema"], output_schema("execution"))
        self.assertIn(agent["instructions"], body["input"][0]["content"])
        self.assertNotIn(_FAKE_KEY, json.dumps(body, ensure_ascii=False))

    def test_supported_rule_kinds_match_analyst_and_strategist_schemas(self):
        agents = {agent["layer"]: agent for agent in _one_agent_per_layer()}
        analyst_manual = agents["analyst"]["instructions"]
        documented_kinds = set(re.findall(r"(?m)^- `([^`]+)`: ", analyst_manual))

        analyst_schema = output_schema("analyst")
        strategist_schema = output_schema("strategist")
        analyst_kinds = set(analyst_schema["properties"]["edges"]["items"]["properties"]["kind"]["enum"])
        strategist_kinds = set(strategist_schema["properties"]["strategies"]["items"]["properties"]["family"]["enum"])

        self.assertEqual(documented_kinds, analyst_kinds)
        self.assertEqual(analyst_kinds, strategist_kinds)

    def test_input_limit_accepts_exact_utf8_boundary_and_rejects_next_byte(self):
        calls = []

        def transport(body, key):
            calls.append((body, key))
            return _completed_response(_EMPTY_OUTPUTS["collector"], body["model"])

        provider = ModelProvider(lambda: _FAKE_KEY, transport)
        exact_payload = _json_payload_of_size(4000)
        exact_encoded = json.dumps(exact_payload, ensure_ascii=False, separators=(",", ":"))
        self.assertEqual(len(exact_encoded.encode("utf-8")), 4000)
        result = provider.run({"layer": "collector", "max_input_bytes": 4000}, exact_payload)
        self.assertIn("output", result)
        self.assertEqual(len(calls), 1)

        over_payload = _json_payload_of_size(4001)
        over_encoded = json.dumps(over_payload, ensure_ascii=False, separators=(",", ":"))
        self.assertEqual(len(over_encoded.encode("utf-8")), 4001)
        with self.assertRaisesRegex(ProviderError, "4000 byte handoff limit"):
            provider.run({"layer": "collector", "max_input_bytes": 4000}, over_payload)
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
