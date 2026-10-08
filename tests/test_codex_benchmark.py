from __future__ import annotations

import json
import unittest

from spotify_pipeline.classify import ModelConfig, actual_cost
from spotify_pipeline.codex_benchmark import deterministic_shards
from spotify_pipeline.providers import ProviderResult, _codex_event_summary, _parse_codex_events


class CodexBenchmarkTests(unittest.TestCase):
    def test_deterministic_shards_keep_exact_duplicates_together(self) -> None:
        rows = [
            {"review_id": "a", "review_text": "same"},
            {"review_id": "b", "review_text": "other"},
            {"review_id": "c", "review_text": "same"},
            {"review_id": "d", "review_text": "third"},
        ]
        first = deterministic_shards(rows, 3)
        second = deterministic_shards(rows, 3)
        self.assertEqual(first, second)
        owners = {
            row["review_id"]: worker
            for worker, shard in enumerate(first)
            for row in shard
        }
        self.assertEqual(owners["a"], owners["c"])

    def test_jsonl_parser_extracts_usage_and_rejectable_tool_type(self) -> None:
        text = "\n".join(
            [
                json.dumps({"type": "thread.started", "thread_id": "thread-1"}),
                json.dumps(
                    {
                        "type": "item.completed",
                        "item": {"type": "command_execution", "command": "pwd"},
                    }
                ),
                json.dumps(
                    {
                        "type": "turn.completed",
                        "usage": {"input_tokens": 10, "output_tokens": 4},
                    }
                ),
            ]
        )
        events, errors = _parse_codex_events(text)
        self.assertEqual(errors, [])
        summary = _codex_event_summary(events)
        self.assertEqual(summary["thread_id"], "thread-1")
        self.assertEqual(summary["usage"]["input_tokens"], 10)
        self.assertEqual(summary["forbidden_tool_types"], ["command_execution"])

    def test_subscription_tokens_are_not_api_priced(self) -> None:
        result = ProviderResult(
            request_id="x",
            payload={},
            input_tokens=1000,
            cached_input_tokens=0,
            output_tokens=100,
            duration_seconds=1,
            provider="codex-exec-subscription",
            billing_mode="chatgpt_subscription",
            usage_status="measured",
        )
        self.assertIsNone(actual_cost(result, ModelConfig()))

    def test_execution_fingerprint_changes_cache_namespace(self) -> None:
        base = ModelConfig(model="m", effort="low")
        codex = ModelConfig(model="m", effort="low", execution_fingerprint="codex-v1")
        self.assertNotEqual(base.label_config, codex.label_config)
        self.assertTrue(codex.label_config.endswith(":codex-v1"))


if __name__ == "__main__":
    unittest.main()
