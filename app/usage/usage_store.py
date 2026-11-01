"""Local request telemetry; deliberately stores no prompts, responses or credentials.

Each operation owns its SQLite connection so callers can safely use asyncio.to_thread.
Prices are copied into a request when it starts; changing preferences never reprices
history. Input tokens include cache tokens, which are charged only once.
"""

from contextlib import contextmanager
import csv
from decimal import Decimal, InvalidOperation
import io
import json
import math
from pathlib import Path
import sqlite3
import time
import uuid


PRICE_KEYS = (
    "input_per_million", "output_per_million",
    "cache_read_per_million", "cache_write_per_million",
)
DEFAULT_PREFERENCES = {**dict.fromkeys(PRICE_KEYS, 0.0), "currency": "CNY"}
STATUSES = {"running", "success", "error", "cancelled", "incomplete"}
TOKEN_KEYS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")
MAX_EXPORT_ROWS = 50_000
MAX_ROUTING_ROWS = 50_000
MAX_INTEGER = 2**63 - 1
BUSY_TIMEOUT_MS = 750
ROUTE_TIERS = {"cheap", "balanced", "coding", "complex", "max", "original"}
ROUTE_CLASSIFIER_MODES = {"qwen", "rules_fallback", "rules_guard", "manual", "pinned"}
# Money columns posted by the router per decision; the order is shared by the
# CREATE TABLE statement, the migration and the CSV export.
ROUTE_COST_COLUMNS = (
    ("cost_currency", "TEXT"),
    ("cost_group", "TEXT"),
    ("cost_group_multiplier", "REAL"),
    ("cost_actual", "REAL"),
    ("cost_baseline", "REAL"),
    ("cost_saved", "REAL"),
    ("cost_actual_base", "REAL"),
    ("cost_baseline_base", "REAL"),
    ("cost_input_tokens", "INTEGER"),
    ("cost_cached_tokens", "INTEGER"),
    ("cost_cache_write_tokens", "INTEGER"),
    ("cost_output_tokens", "INTEGER"),
    ("cost_requests", "INTEGER"),
    ("cost_estimated", "INTEGER"),
    ("cost_source", "TEXT"),
    ("cost_note", "TEXT"),
)
ROUTE_COST_KEYS = tuple(name for name, _ in ROUTE_COST_COLUMNS)


def _number(value, name, *, nullable=False):
    if value is None and nullable:
        return None
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a nonnegative finite number")
    try:
        number = float(value)
    except (ValueError, TypeError, OverflowError) as exc:
        raise ValueError(f"{name} must be a nonnegative finite number") from exc
    if not math.isfinite(number) or number < 0:
        raise ValueError(f"{name} must be a nonnegative finite number")
    return number


def _signed_number(value, name, *, nullable=True):
    """Accept any finite float, including negatives; routing savings may be negative."""
    if value is None and nullable:
        return None
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a finite number")
    try:
        number = float(value)
    except (ValueError, TypeError, OverflowError) as exc:
        raise ValueError(f"{name} must be a finite number") from exc
    if not math.isfinite(number):
        raise ValueError(f"{name} must be a finite number")
    return number


def _integer(value, name, *, low=0, high=MAX_INTEGER, nullable=False):
    if value is None and nullable:
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ValueError(f"{name} must be an integer")
    try:
        number = int(value)
    except (ValueError, TypeError, OverflowError) as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if isinstance(value, str) and value.strip() != str(number):
        raise ValueError(f"{name} must be an integer")
    if not low <= number <= high:
        raise ValueError(f"{name} must be between {low} and {high}")
    return number


def _text(value, name, *, default="", limit=500):
    if value is None:
        value = default
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    if len(value) > limit or "\x00" in value:
        raise ValueError(f"{name} is too long or contains a null character")
    return value


def _as_text(value, name, *, default="", limit=300):
    """The routing/decision string validator (shorter default limit than ``_text``).

    ``UsageStore._route_text`` used to carry its own copy of ``_text``; keeping one
    implementation means a rule such as the null-character check cannot drift
    between the usage records and the routing records.
    """
    if value is None:
        value = default
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    if len(value) > limit or "\x00" in value:
        raise ValueError(f"{name} is too long or contains a null character")
    return value


def _boolean(value, name):
    if type(value) is not bool:
        raise ValueError(f"{name} must be a boolean")
    return value


def _preferences(values, current=None):
    if not isinstance(values, dict):
        raise ValueError("Pricing preferences must be an object")
    if set(values) - set(DEFAULT_PREFERENCES):
        raise ValueError("Unknown pricing preference")
    result = dict(DEFAULT_PREFERENCES if current is None else current)
    for key in PRICE_KEYS:
        if key in values:
            result[key] = _number(values[key], key)
            if result[key] > 1_000_000_000:
                raise ValueError(f"{key} must not exceed 1000000000")
    if "currency" in values:
        if values["currency"] != "CNY":
            raise ValueError("currency must be CNY")
        result["currency"] = values["currency"]
    return result


def _cost(tokens, prices):
    # Keep raw telemetry intact even if an upstream sends inconsistent cache
    # counts, but cap priced cache subsets at the known input total.
    input_tokens = tokens.get("input_tokens")
    read = tokens.get("cache_read_tokens") or 0
    write = tokens.get("cache_write_tokens") or 0
    if input_tokens is not None:
        read = min(read, input_tokens)
        write = min(write, max(0, input_tokens - read))
    ordinary = max(0, (input_tokens or 0) - read - write)
    counts = (ordinary, tokens.get("output_tokens") or 0, read, write)
    try:
        amount = sum(Decimal(count) * Decimal(str(prices[key]))
                     for count, key in zip(counts, PRICE_KEYS)) / Decimal(1_000_000)
        return _number(amount, "cost")
    except (InvalidOperation, OverflowError) as exc:
        raise ValueError("Calculated cost is out of range") from exc


class UsageStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS usage_preferences (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    preferences TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS usage_requests (
                    id TEXT PRIMARY KEY,
                    started_at REAL NOT NULL,
                    finished_at REAL,
                    model TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    source TEXT NOT NULL,
                    stream INTEGER NOT NULL,
                    agent INTEGER NOT NULL,
                    endpoint TEXT NOT NULL,
                    status TEXT NOT NULL,
                    http_status INTEGER,
                    duration_ms REAL,
                    ttft_ms REAL,
                    input_tokens INTEGER,
                    output_tokens INTEGER,
                    cache_read_tokens INTEGER,
                    cache_write_tokens INTEGER,
                    tokens_per_second REAL,
                    error_message TEXT,
                    input_per_million REAL NOT NULL,
                    output_per_million REAL NOT NULL,
                    cache_read_per_million REAL NOT NULL,
                    cache_write_per_million REAL NOT NULL,
                    currency TEXT NOT NULL,
                    cost REAL NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS usage_requests_started
                    ON usage_requests(started_at DESC, id DESC);
                CREATE INDEX IF NOT EXISTS usage_requests_status_started
                    ON usage_requests(status, started_at DESC);
                CREATE INDEX IF NOT EXISTS usage_requests_model
                    ON usage_requests(model);
                CREATE TABLE IF NOT EXISTS routing_decisions (
                    id TEXT PRIMARY KEY,
                    decided_at REAL NOT NULL,
                    source TEXT NOT NULL,
                    router_build TEXT NOT NULL,
                    turn_hash TEXT NOT NULL,
                    decision_mode TEXT NOT NULL,
                    tier TEXT NOT NULL,
                    model TEXT NOT NULL,
                    effort TEXT NOT NULL,
                    decision_reason TEXT NOT NULL,
                    conclusion TEXT NOT NULL,
                    classifier_mode TEXT NOT NULL,
                    classifier_used INTEGER NOT NULL,
                    classifier_reason TEXT,
                    classifier_ms REAL,
                    classifier_kind TEXT,
                    classifier_confidence TEXT,
                    qwen_tier TEXT,
                    qwen_effort TEXT,
                    resource_reason TEXT,
                    resource_allow_qwen INTEGER,
                    gpu_free_mib INTEGER,
                    gpu_threshold_mib INTEGER,
                    comfy_busy INTEGER,
                    comfy_running INTEGER,
                    comfy_pending INTEGER,
                    qwen_state TEXT,
                    qwen_sleeping INTEGER,
                    qwen_active_requests INTEGER,
                    context_messages INTEGER,
                    task_excerpted INTEGER,
                    assessment_json TEXT,
                    resource_json TEXT,
                    cost_currency TEXT,
                    cost_group TEXT,
                    cost_group_multiplier REAL,
                    cost_actual REAL,
                    cost_baseline REAL,
                    cost_saved REAL,
                    cost_actual_base REAL,
                    cost_baseline_base REAL,
                    cost_input_tokens INTEGER,
                    cost_cached_tokens INTEGER,
                    cost_output_tokens INTEGER,
                    cost_requests INTEGER,
                    cost_estimated INTEGER,
                    cost_source TEXT,
                    cost_note TEXT
                );
                CREATE INDEX IF NOT EXISTS routing_decisions_decided
                    ON routing_decisions(decided_at DESC, id DESC);
                CREATE INDEX IF NOT EXISTS routing_decisions_tier
                    ON routing_decisions(tier, decided_at DESC);
            """)
            connection.execute(
                "INSERT OR IGNORE INTO usage_preferences VALUES (1, ?)",
                (json.dumps(DEFAULT_PREFERENCES),),
            )
            columns = {row[1] for row in connection.execute("PRAGMA table_info(routing_decisions)")}
            if "conclusion" not in columns:
                connection.execute(
                    "ALTER TABLE routing_decisions ADD COLUMN conclusion TEXT NOT NULL DEFAULT ''"
                )
            for name, kind in ROUTE_COST_COLUMNS:
                if name not in columns:
                    connection.execute(f"ALTER TABLE routing_decisions ADD COLUMN {name} {kind}")

    @contextmanager
    def _connection(self):
        connection = sqlite3.connect(str(self.path), timeout=BUSY_TIMEOUT_MS / 1000)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
            with connection:
                yield connection
        finally:
            connection.close()

    @staticmethod
    def _read_preferences(connection):
        row = connection.execute(
            "SELECT preferences FROM usage_preferences WHERE singleton = 1"
        ).fetchone()
        return _preferences(json.loads(row[0])) if row else dict(DEFAULT_PREFERENCES)

    def get_preferences(self):
        with self._connection() as connection:
            return self._read_preferences(connection)

    def set_preferences(self, values):
        # Validate before waiting for a write lock; validate the merged object
        # again inside the transaction to retain concurrent preference updates.
        _preferences(values)
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            preferences = _preferences(values, self._read_preferences(connection))
            connection.execute(
                "UPDATE usage_preferences SET preferences = ? WHERE singleton = 1",
                (json.dumps(preferences),),
            )
        return preferences

    def begin(self, record):
        if not isinstance(record, dict):
            raise ValueError("Usage record must be an object")
        request_id = _text(record.get("id", uuid.uuid4().hex), "id", limit=128)
        if not request_id:
            raise ValueError("id must not be empty")
        mode = record.get("mode", "text")
        if not isinstance(mode, str) or mode not in {"text", "vision"}:
            raise ValueError("mode must be text or vision")
        values = {
            "id": request_id,
            "started_at": _number(record.get("started_at", time.time()), "started_at"),
            "model": _text(record.get("model"), "model", default="unknown", limit=300) or "unknown",
            "mode": mode,
            "source": _text(record.get("source"), "source", default="API", limit=40),
            "stream": _boolean(record.get("stream", False), "stream"),
            "agent": _boolean(record.get("agent", False), "agent"),
            "endpoint": _text(record.get("endpoint"), "endpoint", limit=300),
            "status": "running",
        }
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            values.update(self._read_preferences(connection))
            columns = ", ".join(values)
            placeholders = ", ".join("?" for _ in values)
            connection.execute(
                f"INSERT INTO usage_requests ({columns}) VALUES ({placeholders})",
                tuple(values.values()),
            )
        return request_id

    def finish(self, request_id, metrics):
        request_id = _text(request_id, "id", limit=128)
        if not isinstance(metrics, dict):
            raise ValueError("Usage metrics must be an object")
        status = metrics.get("status", "success")
        if not isinstance(status, str) or status not in STATUSES - {"running"}:
            raise ValueError("Finished status must be success, error, cancelled or incomplete")
        values = {
            "status": status,
            "finished_at": _number(metrics.get("finished_at", time.time()), "finished_at"),
            "http_status": _integer(metrics.get("http_status"), "http_status", low=100, high=599, nullable=True),
            "ttft_ms": _number(metrics.get("ttft_ms"), "ttft_ms", nullable=True),
            "tokens_per_second": _number(metrics.get("tokens_per_second"), "tokens_per_second", nullable=True),
        }
        for key in TOKEN_KEYS:
            values[key] = _integer(metrics.get(key), key, nullable=True)
        if "stream" in metrics:
            values["stream"] = _boolean(metrics["stream"], "stream")
        error = metrics.get("error_message")
        if error is not None and not isinstance(error, str):
            raise ValueError("error_message must be a string")
        values["error_message"] = error.replace("\x00", "")[:500] if error else None
        if "duration_ms" in metrics and metrics["duration_ms"] is not None:
            values["duration_ms"] = _number(metrics["duration_ms"], "duration_ms")
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM usage_requests WHERE id = ? AND status = 'running'", (request_id,)
            ).fetchone()
            if row is None:
                return False
            values.setdefault("duration_ms", max(0, values["finished_at"] - row["started_at"]) * 1000)
            values["cost"] = _cost(values, row)
            assignments = ", ".join(f"{column} = ?" for column in values)
            connection.execute(
                f"UPDATE usage_requests SET {assignments} WHERE id = ? AND status = 'running'",
                (*values.values(), request_id),
            )
        return True

    def recover_interrupted(self, finished_at=None):
        """Call once at gateway startup, before accepting new requests."""
        ended = _number(time.time() if finished_at is None else finished_at, "finished_at")
        with self._connection() as connection:
            result = connection.execute("""
                UPDATE usage_requests
                SET status = 'incomplete', finished_at = ?,
                    duration_ms = MAX(0, ? - started_at) * 1000,
                    error_message = '服务重启，未收到该请求的完整结果。'
                WHERE status = 'running'
            """, (ended, ended))
            return result.rowcount

    @staticmethod
    def _filters(filters):
        if filters is None:
            filters = {}
        if not isinstance(filters, dict):
            raise ValueError("Usage filters must be an object")
        allowed = {"start", "end", "model", "mode", "status", "stream", "page", "page_size", "bucket", "timezone_offset_minutes"}
        if set(filters) - allowed:
            raise ValueError("Unknown usage filter")
        end = _number(filters.get("end", time.time()), "end")
        start = _number(filters.get("start", max(0, end - 86400)), "start")
        if end <= start:
            raise ValueError("end must be greater than start")
        page = _integer(filters.get("page", 1), "page", low=1, high=1_000_000_000)
        page_size = _integer(filters.get("page_size", 25), "page_size", low=1, high=100)
        model = _text(filters.get("model"), "model", limit=300).strip()
        mode = filters.get("mode", "all")
        status = filters.get("status", "all")
        stream = filters.get("stream", "all")
        bucket = filters.get("bucket", "hour")
        if not isinstance(mode, str) or mode not in {"all", "text", "vision"}:
            raise ValueError("mode must be all, text or vision")
        if not isinstance(status, str) or status not in STATUSES | {"all"}:
            raise ValueError("Invalid status filter")
        if isinstance(stream, bool):
            stream = "true" if stream else "false"
        if not isinstance(stream, str) or stream not in {"all", "true", "false"}:
            raise ValueError("stream must be all, true or false")
        if not isinstance(bucket, str) or bucket not in {"hour", "day"}:
            raise ValueError("bucket must be hour or day")
        timezone_offset = _integer(filters.get("timezone_offset_minutes", 480),
                                   "timezone_offset_minutes", low=-840, high=840)
        clauses = ["started_at >= ?", "started_at < ?"]
        parameters = [start, end]
        if model:
            clauses.append("model LIKE ? ESCAPE '\\'")
            literal = model.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            parameters.append(f"%{literal}%")
        for key, value in (("mode", mode), ("status", status)):
            if value != "all":
                clauses.append(f"{key} = ?")
                parameters.append(value)
        if stream != "all":
            clauses.append("stream = ?")
            parameters.append(int(stream == "true"))
        normalized = {
            "start": start, "end": end, "page": page, "page_size": page_size,
            "bucket_seconds": 3600 if bucket == "hour" else 86400,
            "timezone_offset_seconds": timezone_offset * 60,
        }
        return normalized, " AND ".join(clauses), parameters

    @staticmethod
    def _row(row):
        result = dict(row)
        result["stream"] = bool(result["stream"])
        result["agent"] = bool(result["agent"])
        result["total_tokens"] = (
            result["input_tokens"] + result["output_tokens"]
            if result["input_tokens"] is not None and result["output_tokens"] is not None else None
        )
        return result

    def query(self, filters=None):
        options, where, parameters = self._filters(filters)
        with self._connection() as connection:
            connection.execute("BEGIN")
            summary = dict(connection.execute(f"""
                SELECT COUNT(*) AS requests,
                    COALESCE(SUM(status = 'success'), 0) AS success,
                    COALESCE(SUM(status = 'error'), 0) AS error,
                    COALESCE(SUM(status = 'cancelled'), 0) AS cancelled,
                    COALESCE(SUM(status = 'incomplete'), 0) AS incomplete,
                    COALESCE(SUM(status = 'running'), 0) AS running,
                    SUM(input_tokens) AS input_tokens,
                    SUM(output_tokens) AS output_tokens,
                    SUM(cache_read_tokens) AS cache_read_tokens,
                    SUM(cache_write_tokens) AS cache_write_tokens,
                    COALESCE(SUM(input_tokens IS NULL OR output_tokens IS NULL), 0) AS unknown_usage_requests,
                    COALESCE(SUM(cost), 0.0) AS cost,
                    AVG(duration_ms) AS duration_avg_ms,
                    AVG(ttft_ms) AS ttft_avg_ms
                FROM usage_requests WHERE {where}
            """, parameters).fetchone())
            if not summary["requests"]:
                for key in TOKEN_KEYS:
                    summary[key] = 0
            summary["total_tokens"] = (
                None if summary["input_tokens"] is None and summary["output_tokens"] is None
                else (summary["input_tokens"] or 0) + (summary["output_tokens"] or 0)
            )
            minutes = max(1, (options["end"] - options["start"]) / 60)
            summary["rpm"] = summary["requests"] / minutes
            summary["tpm"] = summary["total_tokens"] / minutes if summary["total_tokens"] is not None else None
            rows = connection.execute(
                f"SELECT * FROM usage_requests WHERE {where} ORDER BY started_at DESC, id DESC LIMIT ? OFFSET ?",
                (*parameters, options["page_size"], (options["page"] - 1) * options["page_size"]),
            ).fetchall()
            size = options["bucket_seconds"]
            offset = options["timezone_offset_seconds"]
            series = [dict(row) for row in connection.execute(f"""
                SELECT (CAST((started_at + ?) / ? AS INTEGER)
                    - ((started_at + ?) < 0 AND (started_at + ?) / ? != CAST((started_at + ?) / ? AS INTEGER)))
                    * ? - ? AS start, model,
                    COUNT(*) AS requests, COALESCE(SUM(input_tokens), 0) AS input_tokens,
                    COALESCE(SUM(output_tokens), 0) AS output_tokens,
                    COALESCE(SUM(input_tokens), 0) + COALESCE(SUM(output_tokens), 0) AS total_tokens,
                    COALESCE(SUM(cost), 0.0) AS cost
                FROM usage_requests WHERE {where} GROUP BY start, model ORDER BY start, model
            """, (offset, size, offset, offset, size, offset, size, size, offset, *parameters)).fetchall()]
            models = [row[0] for row in connection.execute(
                "SELECT DISTINCT model FROM usage_requests ORDER BY model COLLATE NOCASE"
            ).fetchall()]
        return {
            "rows": [self._row(row) for row in rows], "total": summary["requests"],
            "page": options["page"], "page_size": options["page_size"],
            "summary": summary, "series": series, "models": models,
            "range": {"start": options["start"], "end": options["end"]},
        }

    def csv_rows(self, filters=None):
        """Return spreadsheet-safe CSV text with BOM, ignoring pagination.

        Reject oversized exports explicitly instead of silently dropping history.
        The caller can return this string as text/csv; no disk export is needed.
        """
        _, where, parameters = self._filters(filters)
        with self._connection() as connection:
            rows = connection.execute(
                f"SELECT * FROM usage_requests WHERE {where} ORDER BY started_at DESC, id DESC LIMIT ?",
                (*parameters, MAX_EXPORT_ROWS + 1),
            ).fetchall()
        if len(rows) > MAX_EXPORT_ROWS:
            raise ValueError(f"CSV export is limited to {MAX_EXPORT_ROWS:,} requests; select a shorter time range")
        columns = (
            "id", "started_at", "finished_at", "model", "mode", "source", "stream", "agent",
            "endpoint", "status", "http_status", "input_tokens", "output_tokens", "total_tokens",
            "cache_read_tokens", "cache_write_tokens", "duration_ms", "ttft_ms", "tokens_per_second",
            "cost", "currency", *PRICE_KEYS, "error_message",
        )
        output = io.StringIO(newline="")
        output.write("\ufeff")
        writer = csv.writer(output, lineterminator="\r\n")
        writer.writerow(columns)
        for row in rows:
            record = self._row(row)
            values = []
            for key in columns:
                value = record[key]
                if isinstance(value, str) and value.lstrip(" \t\r\n").startswith(("=", "+", "-", "@")):
                    value = "'" + value
                values.append(value)
            writer.writerow(values)
        return output.getvalue()

    @staticmethod
    def _route_text(value, name, *, default="", limit=300):
        return _as_text(value, name, default=default, limit=limit)

    @staticmethod
    def _route_json(value, name, limit=20_000):
        if value is None:
            return None
        if not isinstance(value, (dict, list)):
            raise ValueError(f"{name} must be an object or array")
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        if len(encoded) > limit:
            raise ValueError(f"{name} is too large")
        return encoded

    def record_route(self, record):
        """Persist one sanitized router decision and trim oldest history."""
        if not isinstance(record, dict):
            raise ValueError("Routing record must be an object")
        tier = self._route_text(record.get("tier"), "tier", limit=40)
        if tier not in ROUTE_TIERS:
            raise ValueError("Invalid routing tier")
        classifier_mode = self._route_text(record.get("classifier_mode"), "classifier_mode", limit=40)
        if classifier_mode not in ROUTE_CLASSIFIER_MODES:
            raise ValueError("Invalid classifier mode")
        values = {
            "id": self._route_text(record.get("id", uuid.uuid4().hex), "id", limit=128),
            "decided_at": _number(record.get("decided_at", time.time()), "decided_at"),
            "source": self._route_text(record.get("source"), "source", default="codex-auto-router", limit=80),
            "router_build": self._route_text(record.get("router_build"), "router_build", limit=80),
            "turn_hash": self._route_text(record.get("turn_hash"), "turn_hash", limit=128),
            "decision_mode": self._route_text(record.get("decision_mode"), "decision_mode", default="primary", limit=40),
            "tier": tier,
            "model": self._route_text(record.get("model"), "model", default="unknown", limit=300) or "unknown",
            "effort": self._route_text(record.get("effort"), "effort", default="high", limit=30),
            "decision_reason": self._route_text(record.get("decision_reason"), "decision_reason", default="unknown", limit=160),
            "conclusion": self._route_text(record.get("conclusion"), "conclusion", limit=500),
            "classifier_mode": classifier_mode,
            "classifier_used": _boolean(record.get("classifier_used", False), "classifier_used"),
            "classifier_reason": self._route_text(record.get("classifier_reason"), "classifier_reason", limit=160) or None,
            "classifier_ms": _number(record.get("classifier_ms"), "classifier_ms", nullable=True),
            "classifier_kind": self._route_text(record.get("classifier_kind"), "classifier_kind", limit=60) or None,
            "classifier_confidence": self._route_text(record.get("classifier_confidence"), "classifier_confidence", limit=30) or None,
            "qwen_tier": self._route_text(record.get("qwen_tier"), "qwen_tier", limit=40) or None,
            "qwen_effort": self._route_text(record.get("qwen_effort"), "qwen_effort", limit=30) or None,
            "resource_reason": self._route_text(record.get("resource_reason"), "resource_reason", limit=160) or None,
            "resource_allow_qwen": record.get("resource_allow_qwen"),
            "gpu_free_mib": record.get("gpu_free_mib"),
            "gpu_threshold_mib": record.get("gpu_threshold_mib"),
            "comfy_busy": record.get("comfy_busy"),
            "comfy_running": record.get("comfy_running"),
            "comfy_pending": record.get("comfy_pending"),
            "qwen_state": self._route_text(record.get("qwen_state"), "qwen_state", limit=40) or None,
            "qwen_sleeping": record.get("qwen_sleeping"),
            "qwen_active_requests": record.get("qwen_active_requests"),
            "context_messages": record.get("context_messages"),
            "task_excerpted": record.get("task_excerpted"),
            "assessment_json": self._route_json(record.get("assessment"), "assessment"),
            "resource_json": self._route_json(record.get("resource"), "resource"),
            "cost_currency": self._route_text(record.get("cost_currency"), "cost_currency", limit=40) or None,
            "cost_group": self._route_text(record.get("cost_group"), "cost_group", limit=40) or None,
            "cost_group_multiplier": _number(record.get("cost_group_multiplier"), "cost_group_multiplier", nullable=True),
            "cost_actual": _number(record.get("cost_actual"), "cost_actual", nullable=True),
            "cost_baseline": _number(record.get("cost_baseline"), "cost_baseline", nullable=True),
            "cost_saved": _signed_number(record.get("cost_saved"), "cost_saved"),
            "cost_actual_base": _number(record.get("cost_actual_base"), "cost_actual_base", nullable=True),
            "cost_baseline_base": _number(record.get("cost_baseline_base"), "cost_baseline_base", nullable=True),
            "cost_input_tokens": _integer(record.get("cost_input_tokens"), "cost_input_tokens", nullable=True),
            "cost_cached_tokens": _integer(record.get("cost_cached_tokens"), "cost_cached_tokens", nullable=True),
            "cost_cache_write_tokens": _integer(record.get("cost_cache_write_tokens"), "cost_cache_write_tokens", nullable=True),
            "cost_output_tokens": _integer(record.get("cost_output_tokens"), "cost_output_tokens", nullable=True),
            "cost_requests": _integer(record.get("cost_requests"), "cost_requests", nullable=True),
            "cost_estimated": record.get("cost_estimated"),
            "cost_source": self._route_text(record.get("cost_source"), "cost_source", limit=120) or None,
            "cost_note": self._route_text(record.get("cost_note"), "cost_note", limit=240) or None,
        }
        for key in ("resource_allow_qwen", "comfy_busy", "qwen_sleeping", "task_excerpted"):
            if values[key] is not None:
                values[key] = int(_boolean(values[key], key))
        if values["cost_estimated"] is not None:
            values["cost_estimated"] = int(_boolean(values["cost_estimated"], "cost_estimated"))
        for key in ("gpu_free_mib", "gpu_threshold_mib", "comfy_running", "comfy_pending", "qwen_active_requests", "context_messages"):
            if values[key] is not None:
                values[key] = _integer(values[key], key)
        if not values["id"] or not values["turn_hash"] or not values["router_build"]:
            raise ValueError("Routing identifiers must not be empty")
        if not values['conclusion'].strip():
            raise ValueError('Routing conclusion must not be empty')
        columns = ", ".join(values)
        placeholders = ", ".join("?" for _ in values)
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                f"INSERT OR REPLACE INTO routing_decisions ({columns}) VALUES ({placeholders})",
                tuple(values.values()),
            )
            connection.execute("""
                DELETE FROM routing_decisions
                WHERE id IN (
                    SELECT id FROM routing_decisions
                    ORDER BY decided_at ASC, id ASC
                    LIMIT MAX(0, (SELECT COUNT(*) FROM routing_decisions) - ?)
                )
            """, (MAX_ROUTING_ROWS,))
        return values["id"]

    @staticmethod
    def _routing_filters(filters):
        if filters is None:
            filters = {}
        if not isinstance(filters, dict):
            raise ValueError("Routing filters must be an object")
        allowed = {"start", "end", "tier", "model", "classifier_mode", "reason", "page", "page_size"}
        if set(filters) - allowed:
            raise ValueError("Unknown routing filter")
        end = _number(filters.get("end", time.time()), "end")
        start = _number(filters.get("start", max(0, end - 86400)), "start")
        if end <= start:
            raise ValueError("end must be greater than start")
        page = _integer(filters.get("page", 1), "page", low=1, high=1_000_000_000)
        page_size = _integer(filters.get("page_size", 50), "page_size", low=1, high=100)
        tier = UsageStore._route_text(filters.get("tier"), "tier", limit=40).strip()
        classifier_mode = UsageStore._route_text(filters.get("classifier_mode"), "classifier_mode", limit=40).strip()
        model = UsageStore._route_text(filters.get("model"), "model", limit=300).strip()
        reason = UsageStore._route_text(filters.get("reason"), "reason", limit=160).strip()
        if tier and tier not in ROUTE_TIERS:
            raise ValueError("Invalid routing tier filter")
        if classifier_mode and classifier_mode not in ROUTE_CLASSIFIER_MODES:
            raise ValueError("Invalid classifier mode filter")
        clauses = ["decided_at >= ?", "decided_at < ?"]
        parameters = [start, end]
        for key, value in (("tier", tier), ("classifier_mode", classifier_mode)):
            if value:
                clauses.append(f"{key} = ?")
                parameters.append(value)
        if model:
            clauses.append("model LIKE ? ESCAPE '\\'")
            literal = model.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            parameters.append(f"%{literal}%")
        if reason:
            clauses.append("(decision_reason LIKE ? ESCAPE '\\' OR classifier_reason LIKE ? ESCAPE '\\' OR conclusion LIKE ? ESCAPE '\\')")
            literal = reason.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            parameters.extend([f"%{literal}%"] * 3)
        return {"start": start, "end": end, "page": page, "page_size": page_size}, " AND ".join(clauses), parameters

    @staticmethod
    def _route_row(row):
        result = dict(row)
        for key in ("classifier_used", "resource_allow_qwen", "comfy_busy", "qwen_sleeping", "task_excerpted", "cost_estimated"):
            if result[key] is not None:
                result[key] = bool(result[key])
        for key in ("assessment_json", "resource_json"):
            raw = result.pop(key)
            result[key[:-5]] = json.loads(raw) if raw else None
        return result

    def query_routes(self, filters=None):
        options, where, parameters = self._routing_filters(filters)
        with self._connection() as connection:
            summary = dict(connection.execute(f"""
                SELECT COUNT(*) AS routes,
                    COALESCE(SUM(classifier_mode = 'qwen'), 0) AS qwen_routes,
                    COALESCE(SUM(classifier_mode IN ('rules_fallback', 'rules_guard')), 0) AS fallback_routes,
                    COALESCE(SUM(tier = 'max'), 0) AS max_routes,
                    COALESCE(SUM(tier = 'complex'), 0) AS complex_routes,
                    COALESCE(SUM(tier = 'coding'), 0) AS coding_routes,
                    COALESCE(SUM(cost_actual IS NOT NULL), 0) AS cost_routes,
                    SUM(cost_actual) AS cost_actual_total,
                    SUM(cost_baseline) AS cost_baseline_total,
                    SUM(cost_saved) AS cost_saved_total
                FROM routing_decisions WHERE {where}
            """, parameters).fetchone())
            currency = connection.execute(f"""
                SELECT cost_currency FROM routing_decisions
                WHERE {where} AND cost_currency IS NOT NULL AND cost_currency != ''
                ORDER BY decided_at DESC, id DESC LIMIT 1
            """, parameters).fetchone()
            summary["cost_currency"] = currency[0] if currency else None
            rows = connection.execute(
                f"SELECT * FROM routing_decisions WHERE {where} ORDER BY decided_at DESC, id DESC LIMIT ? OFFSET ?",
                (*parameters, options["page_size"], (options["page"] - 1) * options["page_size"]),
            ).fetchall()
        return {
            "rows": [self._route_row(row) for row in rows],
            "total": summary["routes"],
            "page": options["page"],
            "page_size": options["page_size"],
            "summary": summary,
            "range": {"start": options["start"], "end": options["end"]},
        }

    def csv_route_rows(self, filters=None):
        _, where, parameters = self._routing_filters(filters)
        with self._connection() as connection:
            rows = connection.execute(
                f"SELECT * FROM routing_decisions WHERE {where} ORDER BY decided_at DESC, id DESC LIMIT ?",
                (*parameters, MAX_EXPORT_ROWS + 1),
            ).fetchall()
        if len(rows) > MAX_EXPORT_ROWS:
            raise ValueError(f"CSV export is limited to {MAX_EXPORT_ROWS:,} routing decisions; select a shorter time range")
        columns = ("id", "decided_at", "source", "router_build", "turn_hash", "decision_mode", "tier", "model", "effort",
                   "decision_reason", "conclusion", "classifier_mode", "classifier_used", "classifier_reason", "classifier_ms",
                   "classifier_kind", "classifier_confidence", "qwen_tier", "qwen_effort", "resource_reason",
                   "resource_allow_qwen", "gpu_free_mib", "gpu_threshold_mib", "comfy_busy", "comfy_running",
                   "comfy_pending", "qwen_state", "qwen_sleeping", "qwen_active_requests", "context_messages", "task_excerpted",
                   "assessment", "resource", *ROUTE_COST_KEYS)
        output = io.StringIO(newline="")
        output.write("\ufeff")
        writer = csv.writer(output, lineterminator="\r\n")
        writer.writerow(columns)
        for row in rows:
            record = self._route_row(row)
            values = []
            for key in columns:
                value = record[key]
                if isinstance(value, (dict, list)):
                    value = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
                if isinstance(value, str) and value.lstrip(" \t\r\n").startswith(("=", "+", "-", "@")):
                    value = "'" + value
                values.append(value)
            writer.writerow(values)
        return output.getvalue()
