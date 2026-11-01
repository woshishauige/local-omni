"""Usage telemetry tests use a temporary SQLite database, never live history."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import csv
import io
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch

from app.usage.usage_store import ROUTE_COST_KEYS, UsageStore


class UsageStoreTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="qwen-usage-")
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "logs" / "usage.sqlite3"
        self.store = UsageStore(self.path)

    def begin(self, request_id="one", started_at=1000, **extra):
        return self.store.begin({"id": request_id, "started_at": started_at,
                                 "model": "qwen-local", "mode": "text", **extra})

    def finish(self, request_id="one", **extra):
        return self.store.finish(request_id, {"finished_at": 1002, "status": "success",
                                             "http_status": 200, **extra})

    def query(self, **extra):
        return self.store.query({"start": 0, "end": 7200, **extra})

    def test_empty_and_default_window(self):
        with patch("app.usage.usage_store.time.time", return_value=200000):
            result = self.store.query()
        self.assertEqual(result["range"], {"start": 113600, "end": 200000})
        self.assertEqual(result["total"], 0)
        self.assertEqual(result["rows"], [])
        self.assertEqual(result["series"], [])
        self.assertEqual(result["summary"]["total_tokens"], 0)
        self.assertEqual(result["summary"]["rpm"], 0)
        self.assertIsNone(result["summary"]["duration_avg_ms"])
        with closing(sqlite3.connect(self.path)) as connection:
            self.assertEqual(connection.execute("PRAGMA journal_mode").fetchone()[0], "wal")

    def test_running_finish_null_counts_and_idempotent_terminal_status(self):
        request_id = self.begin(stream=True, agent=True, source="Web")
        row = self.query()["rows"][0]
        self.assertEqual(row["status"], "running")
        self.assertIsNone(row["input_tokens"])
        self.assertIs(row["stream"], True)
        self.assertTrue(self.finish(request_id, output_tokens=10, ttft_ms=120))
        self.assertFalse(self.finish(request_id, status="cancelled", output_tokens=99))
        self.assertFalse(self.finish("absent"))
        result = self.query()
        row = result["rows"][0]
        self.assertEqual(row["status"], "success")
        self.assertEqual(row["duration_ms"], 2000)
        self.assertIsNone(row["total_tokens"])
        self.assertEqual(result["summary"]["unknown_usage_requests"], 1)
        self.assertEqual(result["summary"]["output_tokens"], 10)
        self.assertEqual(result["summary"]["ttft_avg_ms"], 120)

    def test_price_snapshots_and_cache_subsets_are_not_double_counted(self):
        prices = {"input_per_million": 10, "output_per_million": 20,
                  "cache_read_per_million": 1, "cache_write_per_million": 3}
        self.store.set_preferences(prices)
        self.begin()
        self.store.set_preferences({"input_per_million": 1000, "output_per_million": 2000})
        self.finish(input_tokens=1000, output_tokens=200, cache_read_tokens=400, cache_write_tokens=100)
        row = self.query()["rows"][0]
        expected = (500 * 10 + 200 * 20 + 400 * 1 + 100 * 3) / 1_000_000
        self.assertAlmostEqual(row["cost"], expected)
        self.assertEqual(row["input_per_million"], 10)
        self.assertEqual(row["total_tokens"], 1200)
        self.assertEqual(self.store.get_preferences()["cache_read_per_million"], 1)
        self.assertEqual(UsageStore(self.path).get_preferences()["input_per_million"], 1000)
        self.begin("two")
        self.finish("two", input_tokens=10, output_tokens=0, cache_read_tokens=20, cache_write_tokens=20)
        second = next(row for row in self.query()["rows"] if row["id"] == "two")
        self.assertAlmostEqual(second["cost"], 10 / 1_000_000)

    def test_default_cost_zero_and_known_zero_usage_is_distinct_from_unknown(self):
        self.begin()
        self.finish(input_tokens=0, output_tokens=0, cache_read_tokens=0, cache_write_tokens=0)
        result = self.query()
        self.assertEqual(result["summary"]["cost"], 0)
        self.assertEqual(result["summary"]["unknown_usage_requests"], 0)
        self.assertEqual(result["rows"][0]["total_tokens"], 0)

    def test_filters_paging_summary_and_time_boundaries(self):
        for i, (status, mode) in enumerate((("success", "text"), ("error", "vision"),
                                          ("cancelled", "text"), ("incomplete", "vision"))):
            self.begin(str(i), started_at=1000 + i, mode=mode, stream=bool(i % 2),
                       model="qwen-V" if mode == "vision" else "qwen-T")
            self.finish(str(i), status=status, input_tokens=10, output_tokens=5, finished_at=1010)
        result = self.query(page=2, page_size=2)
        self.assertEqual([row["id"] for row in result["rows"]], ["1", "0"])
        self.assertEqual(result["total"], 4)
        self.assertEqual(result["summary"]["total_tokens"], 60)
        self.assertEqual(result["models"], ["qwen-T", "qwen-V"])
        self.assertEqual(self.query(mode="vision", stream="true")["total"], 2)
        self.assertEqual(self.query(status="error")["rows"][0]["id"], "1")
        self.assertEqual(self.query(start=1001, end=1003)["total"], 2)
        self.assertEqual(self.query(model="qwen-v")["total"], 2)
        self.assertEqual(self.query(model="' OR 1=1 --")["total"], 0)
        for state in ("success", "error", "cancelled", "incomplete"):
            self.assertEqual(result["summary"][state], 1)

    def test_model_substring_filter_treats_sql_wildcards_literally(self):
        self.begin("one", model="qwen_100%")
        self.begin("two", model="qwenX100Y")
        self.assertEqual(self.query(model="_100%")["total"], 1)

    def test_chart_buckets_and_rates_cover_whole_filtered_range(self):
        self.begin("a", started_at=3599, model="A")
        self.finish("a", input_tokens=100, output_tokens=50, finished_at=3600)
        self.begin("b", started_at=3600, model="B")
        self.finish("b", input_tokens=10, output_tokens=20, finished_at=3602)
        result = self.query(page_size=1)
        self.assertEqual([(item["start"], item["model"]) for item in result["series"]], [(0, "A"), (3600, "B")])
        self.assertEqual(sum(item["total_tokens"] for item in result["series"]), 180)
        self.assertAlmostEqual(result["summary"]["rpm"], 2 / 120)
        self.assertAlmostEqual(result["summary"]["tpm"], 180 / 120)
        daily = self.query(bucket="day")
        self.assertEqual({item["start"] for item in daily["series"]}, {-28800})

    def test_buckets_follow_requested_timezone_and_short_window_rates_are_bounded(self):
        self.begin("a", started_at=57600)
        self.finish("a", input_tokens=30, output_tokens=10, finished_at=57601)
        result = self.query(start=57599, end=57602, bucket="day")
        self.assertEqual(result["series"][0]["start"], 57600)
        self.assertEqual(result["summary"]["rpm"], 1)
        self.assertEqual(result["summary"]["tpm"], 40)
        shifted = self.query(start=0, end=86400, bucket="day", timezone_offset_minutes=-480)
        self.assertEqual(shifted["series"][0]["start"], 28800)
        self.begin("b", started_at=1)
        early = self.query(start=0, end=10, bucket="hour", timezone_offset_minutes=-30)
        self.assertEqual(early["series"][0]["start"], -1800)

    def test_restart_marks_only_running_records_incomplete(self):
        self.begin("finished")
        self.finish("finished", input_tokens=1, output_tokens=1)
        self.begin("interrupted")
        restarted = UsageStore(self.path)
        self.assertEqual(restarted.recover_interrupted(finished_at=1010), 1)
        self.assertEqual(restarted.recover_interrupted(finished_at=1020), 0)
        rows = {row["id"]: row for row in self.query()["rows"]}
        self.assertEqual(rows["finished"]["status"], "success")
        self.assertEqual(rows["interrupted"]["status"], "incomplete")
        self.assertEqual(rows["interrupted"]["duration_ms"], 10000)
        self.assertIsNone(rows["interrupted"]["output_tokens"])

    def test_csv_quotes_unicode_blocks_formulas_and_ignores_pagination(self):
        self.begin("one", model='=HYPERLINK("https://invalid.test")')
        self.finish(error_message="中文,错误\n下一行")
        self.begin("two", model="正常模型")
        exported = self.store.csv_rows({"start": 0, "end": 7200, "page_size": 1})
        self.assertTrue(exported.startswith("\ufeff"))
        rows = list(csv.DictReader(io.StringIO(exported.lstrip("\ufeff"))))
        self.assertEqual(len(rows), 2)
        formula = next(row for row in rows if row["id"] == "one")
        self.assertTrue(formula["model"].startswith("'="))
        self.assertEqual(formula["error_message"], "中文,错误\n下一行")
        with patch("app.usage.usage_store.MAX_EXPORT_ROWS", 1):
            with self.assertRaisesRegex(ValueError, "shorter time range"):
                self.store.csv_rows({"start": 0, "end": 7200})

    def test_whitelist_keeps_content_and_secrets_out_of_database(self):
        secret = "SENTINEL_DO_NOT_STORE_39b00a"
        self.begin(prompt=secret, messages=[{"content": secret}], api_key=secret)
        self.finish(input_tokens=1, output_tokens=1, response=secret, authorization=secret)
        with closing(sqlite3.connect(self.path)) as connection:
            dump = "\n".join(connection.iterdump())
        self.assertNotIn(secret, dump)

    def test_rejects_invalid_values_without_poisoning_history(self):
        for invalid in (-1, float("nan"), float("inf"), True, "oops", None):
            with self.subTest(price=invalid), self.assertRaises(ValueError):
                self.store.set_preferences({"input_per_million": invalid})
        for prefs in ({"currency": "USD"}, {"unknown": 1}, []):
            with self.subTest(prefs=prefs), self.assertRaises(ValueError):
                self.store.set_preferences(prefs)
        for filters in ({"page": 0}, {"page_size": 101}, {"page_size": 1.5},
                        {"status": "wat"}, {"mode": "agent"}, {"stream": "0"},
                        {"bucket": "minute"}, {"start": 0, "end": 0}, {"end": "nan"}, {"bad": 1},
                        {"timezone_offset_minutes": 841}, {"mode": []}, {"stream": []}):
            with self.subTest(filters=filters), self.assertRaises(ValueError):
                self.store.query(filters)
        self.begin()
        for metrics in ({"input_tokens": -1}, {"input_tokens": True}, {"output_tokens": 1.1},
                        {"duration_ms": float("inf")}, {"status": "running"}, {"http_status": 99}):
            with self.subTest(metrics=metrics), self.assertRaises(ValueError):
                self.finish(**metrics)
        self.assertEqual(self.query()["rows"][0]["status"], "running")
        self.finish(error_message="错" * 800)
        self.assertEqual(len(self.query()["rows"][0]["error_message"]), 500)

    def test_concurrent_threads_and_reopen_preserve_all_records(self):
        def record(index):
            self.begin(str(index), started_at=1000 + index)
            self.finish(str(index), finished_at=1100, input_tokens=index, output_tokens=1)
            return self.query(page_size=1)["total"]

        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(record, range(32)))
        reopened = UsageStore(self.path)
        result = reopened.query({"start": 0, "end": 7200})
        self.assertEqual(result["total"], 32)
        self.assertEqual(result["summary"]["input_tokens"], sum(range(32)))
        self.assertEqual(result["summary"]["success"], 32)

    def test_locked_database_fails_promptly_and_next_request_can_succeed(self):
        with closing(sqlite3.connect(self.path)) as blocker:
            blocker.execute("BEGIN IMMEDIATE")
            started = time.monotonic()
            with self.assertRaisesRegex(sqlite3.OperationalError, "locked"):
                self.begin("blocked")
            self.assertLess(time.monotonic() - started, 2.5)
            blocker.rollback()
        self.begin("next")
        self.assertEqual(self.query()["total"], 1)

    def test_routing_decision_keeps_concrete_conclusion_and_filters_it(self):
        record = {
            "id": "route-1", "decided_at": 1000, "source": "codex-auto-router", "router_build": "build-1",
            "turn_hash": "turn-1", "decision_mode": "primary", "tier": "coding", "model": "gpt-5.6-sol",
            "effort": "high", "decision_reason": "qwen_primary", "conclusion": "Qwen conclusion: concrete multi-step verification; use coding / high.",
            "classifier_mode": "qwen", "classifier_used": True, "classifier_reason": "qwen_primary",
            "classifier_kind": "coding", "classifier_confidence": "high", "qwen_tier": "coding", "qwen_effort": "high",
            "context_messages": 3, "assessment": {"signals": ["multiple_components"]},
            "resource": {"reason": "resources_available", "allow_qwen": True},
        }
        self.store.record_route(record)
        result = self.store.query_routes({"start": 0, "end": 2000, "reason": "concrete multi-step"})
        self.assertEqual(result["total"], 1)
        self.assertIn("concrete", result["rows"][0]["conclusion"])
        exported = self.store.csv_route_rows({"start": 0, "end": 2000})
        self.assertIn("conclusion", exported.splitlines()[0])
        self.store.record_route(record)
        self.assertEqual(self.store.query_routes({'start': 0, 'end': 2000})['total'], 1)
        with patch('app.usage.usage_store.MAX_ROUTING_ROWS', 2):
            self.store.record_route(dict(record, id='route-2', decided_at=1001))
            self.store.record_route(dict(record, id='route-3', decided_at=1002))
        rows = self.store.query_routes({'start': 0, 'end': 2000})['rows']
        self.assertEqual([r['id'] for r in rows], ['route-3', 'route-2'])

    def test_routing_decision_requires_conclusion(self):
        with self.assertRaisesRegex(ValueError, "conclusion"):
            self.store.record_route({"id": "missing-conclusion", "decided_at": 1, "source": "test", "router_build": "build",
                                     "turn_hash": "turn", "tier": "cheap", "model": "model", "effort": "low",
                                     "classifier_mode": "qwen", "conclusion": ""})

    def route(self, record_id, decided_at, **extra):
        return {"id": record_id, "decided_at": decided_at, "source": "codex-auto-router", "router_build": "build-1",
                "turn_hash": "turn-" + record_id, "decision_mode": "primary", "tier": "coding", "model": "gpt-5.6-sol",
                "effort": "high", "decision_reason": "qwen_primary", "conclusion": "使用 coding / high。",
                "classifier_mode": "qwen", "classifier_used": True, **extra}

    def routes(self, **extra):
        return self.store.query_routes({"start": 0, "end": 9000, **extra})

    def test_routing_cost_fields_aggregate_from_newest_currency_and_export(self):
        self.store.record_route(self.route("legacy", 1000))
        self.store.record_route(self.route(
            "priced", 2000, cost_currency="CNY", cost_group="default", cost_group_multiplier=1.5,
            cost_actual=2.52, cost_baseline=2.10, cost_saved=-0.42, cost_actual_base=1.68, cost_baseline_base=1.40,
            cost_input_tokens=1200, cost_cached_tokens=400, cost_cache_write_tokens=150, cost_output_tokens=800, cost_requests=2,
            cost_estimated=True, cost_source="config", cost_note="astra 缓存单价按输入 1/10 估算"))
        self.store.record_route(self.route(
            "counted", 3000, cost_input_tokens=100, cost_cached_tokens=0, cost_cache_write_tokens=0, cost_output_tokens=20, cost_requests=1))
        self.store.record_route(self.route("legacy-newest", 4000))
        result = self.routes()
        self.assertEqual(result["total"], 4)
        rows = {row["id"]: row for row in result["rows"]}
        priced = rows["priced"]
        self.assertEqual(priced["cost_currency"], "CNY")
        self.assertEqual(priced["cost_group"], "default")
        self.assertEqual(priced["cost_group_multiplier"], 1.5)
        self.assertEqual(priced["cost_actual"], 2.52)
        self.assertEqual(priced["cost_baseline"], 2.10)
        self.assertEqual(priced["cost_saved"], -0.42)
        self.assertEqual(priced["cost_actual_base"], 1.68)
        self.assertEqual(priced["cost_baseline_base"], 1.40)
        self.assertEqual(priced["cost_input_tokens"], 1200)
        self.assertEqual(priced["cost_cached_tokens"], 400)
        self.assertEqual(priced["cost_output_tokens"], 800)
        self.assertEqual(priced["cost_cache_write_tokens"], 150)
        self.assertEqual(priced["cost_requests"], 2)
        self.assertIs(priced["cost_estimated"], True)
        self.assertEqual(priced["cost_source"], "config")
        self.assertEqual(priced["cost_note"], "astra 缓存单价按输入 1/10 估算")
        counted = rows["counted"]
        self.assertIsNone(counted["cost_actual"])
        self.assertIsNone(counted["cost_baseline"])
        self.assertIsNone(counted["cost_saved"])
        self.assertIsNone(counted["cost_estimated"])
        self.assertEqual((counted["cost_input_tokens"], counted["cost_cached_tokens"], counted["cost_cache_write_tokens"], counted["cost_output_tokens"]), (100, 0, 0, 20))
        for key in ("cost_actual", "cost_baseline", "cost_saved", "cost_currency", "cost_group", "cost_group_multiplier",
                    "cost_actual_base", "cost_baseline_base", "cost_input_tokens", "cost_estimated", "cost_source", "cost_note"):
            self.assertIsNone(rows["legacy-newest"][key])
        summary = result["summary"]
        self.assertEqual(summary["cost_routes"], 1)
        self.assertEqual(summary["cost_actual_total"], 2.52)
        self.assertEqual(summary["cost_baseline_total"], 2.10)
        self.assertEqual(summary["cost_saved_total"], -0.42)
        self.assertEqual(summary["cost_currency"], "CNY")
        unpriced = self.store.query_routes({"start": 3000, "end": 4001})["summary"]
        self.assertEqual(unpriced["cost_routes"], 0)
        self.assertIsNone(unpriced["cost_actual_total"])
        self.assertIsNone(unpriced["cost_baseline_total"])
        self.assertIsNone(unpriced["cost_saved_total"])
        self.assertIsNone(unpriced["cost_currency"])
        cost_columns = ("cost_currency", "cost_group", "cost_group_multiplier", "cost_actual", "cost_baseline", "cost_saved",
                        "cost_actual_base", "cost_baseline_base", "cost_input_tokens", "cost_cached_tokens", "cost_cache_write_tokens", "cost_output_tokens",
                        "cost_requests", "cost_estimated", "cost_source", "cost_note")
        exported = self.store.csv_route_rows({"start": 0, "end": 9000})
        header = exported.lstrip("\ufeff").splitlines()[0].split(",")
        self.assertEqual(header[-len(cost_columns):], list(cost_columns))
        self.assertEqual(header[-len(cost_columns) - 1], "resource")
        exported_rows = {row["id"]: row for row in csv.DictReader(io.StringIO(exported.lstrip("\ufeff")))}
        self.assertEqual(exported_rows["priced"]["cost_saved"], "-0.42")
        self.assertEqual(exported_rows["priced"]["cost_estimated"], "True")
        self.assertEqual(exported_rows["priced"]["cost_note"], "astra 缓存单价按输入 1/10 估算")
        self.assertEqual(exported_rows["counted"]["cost_actual"], "")
        self.assertEqual(exported_rows["legacy"]["cost_currency"], "")

    def test_routing_cost_validation_rejects_invalid_values(self):
        for key, invalid in (("cost_actual", -0.01), ("cost_baseline", float("nan")), ("cost_group_multiplier", float("inf")),
                             ("cost_actual_base", True), ("cost_baseline_base", "oops"), ("cost_saved", float("inf")),
                             ("cost_saved", True), ("cost_input_tokens", -1), ("cost_requests", 1.5), ("cost_estimated", "true"),
                             ("cost_currency", 7), ("cost_currency", "C" * 41), ("cost_group", "G" * 41), ("cost_source", "=" * 121),
                             ("cost_note", "注" * 241)):
            with self.subTest(field=key, value=invalid), self.assertRaises(ValueError):
                self.store.record_route(self.route("bad-" + key, 1000, **{key: invalid}))
        self.assertEqual(self.routes()["total"], 0)
        self.store.record_route(self.route("signed", 1000, cost_saved=-1.25, cost_actual=0))
        self.assertEqual(self.routes()["rows"][0]["cost_saved"], -1.25)

    def test_existing_database_gains_cost_columns_and_keeps_history(self):
        legacy_path = self.path.parent / "legacy.sqlite3"
        legacy_schema = """
            CREATE TABLE routing_decisions (
                id TEXT PRIMARY KEY, decided_at REAL NOT NULL, source TEXT NOT NULL, router_build TEXT NOT NULL,
                turn_hash TEXT NOT NULL, decision_mode TEXT NOT NULL, tier TEXT NOT NULL, model TEXT NOT NULL,
                effort TEXT NOT NULL, decision_reason TEXT NOT NULL, classifier_mode TEXT NOT NULL,
                classifier_used INTEGER NOT NULL, classifier_reason TEXT, classifier_ms REAL, classifier_kind TEXT,
                classifier_confidence TEXT, qwen_tier TEXT, qwen_effort TEXT, resource_reason TEXT,
                resource_allow_qwen INTEGER, gpu_free_mib INTEGER, gpu_threshold_mib INTEGER, comfy_busy INTEGER,
                comfy_running INTEGER, comfy_pending INTEGER, qwen_state TEXT, qwen_sleeping INTEGER,
                qwen_active_requests INTEGER, context_messages INTEGER, task_excerpted INTEGER,
                assessment_json TEXT, resource_json TEXT
            )
        """
        with closing(sqlite3.connect(legacy_path)) as connection, connection:
            connection.execute(legacy_schema)
            connection.execute(
                "INSERT INTO routing_decisions (id, decided_at, source, router_build, turn_hash, decision_mode, tier,"
                " model, effort, decision_reason, classifier_mode, classifier_used)"
                " VALUES ('old', 1000, 'codex-auto-router', 'build-1', 'turn-old', 'primary', 'cheap', 'gpt-5.6-luna',"
                " 'low', 'qwen_primary', 'qwen', 1)"
            )
        migrated = UsageStore(legacy_path)
        with closing(sqlite3.connect(legacy_path)) as connection:
            columns = {row[1] for row in connection.execute("PRAGMA table_info(routing_decisions)")}
        self.assertTrue({"conclusion", *ROUTE_COST_KEYS} <= columns)
        migrated.record_route(self.route("new", 2000, cost_currency="CNY", cost_actual=0.5, cost_saved=1.5))
        rows = {row["id"]: row for row in migrated.query_routes({"start": 0, "end": 9000})["rows"]}
        self.assertEqual(sorted(rows), ["new", "old"])
        self.assertEqual(rows["old"]["conclusion"], "")
        for key in ROUTE_COST_KEYS:
            self.assertIsNone(rows["old"][key])
        self.assertEqual((rows["new"]["cost_actual"], rows["new"]["cost_saved"]), (0.5, 1.5))


if __name__ == "__main__":
    unittest.main()
