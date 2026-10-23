from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from functools import lru_cache
from pathlib import Path

import anyio


def now() -> str:
    return datetime.now(UTC).isoformat()


REQUEST_FIELDS = frozenset(
    {
        "request_id",
        "app_id",
        "subject",
        "principal_type",
        "authorized_party",
        "auth_method",
        "scene",
        "correlation_id",
        "entry",
        "requested_model",
        "model",
        "generation_id",
        "outcome",
        "error_code",
        "duration_ms",
        "credential_alias",
        "credential_resource_id",
        "output_size",
        "artifact_app_id",
        "artifact_subject",
        "price_version",
    }
)
ATTEMPT_FIELDS = frozenset(
    {
        "model",
        "provider",
        "credential_alias",
        "credential_resource_id",
        "outcome",
        "http_status",
        "upstream_request_id",
        "duration_ms",
        "estimated_cost_cny",
        "price_version",
        "output_size",
        "input_images",
        "generated_images",
        "output_tokens",
        "total_tokens",
    }
)


class UsageStore:
    """SQLite WAL 记录；提交上游前先落盘，容器重启仍能识别悬而未决的尝试。"""

    def __init__(self, path: str, retention_days: int = 90):
        self.path = path
        self.retention_days = max(90, retention_days)
        self._lock = threading.Lock()
        self._initialized = False
        self._last_prune = 0.0

    def _connect(self):
        # 同进程串行化初始化/写入；跨进程依靠 SQLite 的短锁等待。
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=0.15)
        try:
            self._prepare(db)
        except BaseException:
            db.close()
            raise
        return db

    def _prepare(self, db) -> None:
        deadline = time.monotonic() + 0.2
        db.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
        db.execute("PRAGMA foreign_keys=ON")
        if not self._initialized:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS requests (
                    request_id TEXT PRIMARY KEY, app_id TEXT NOT NULL, started_at TEXT NOT NULL,
                    outcome TEXT NOT NULL DEFAULT 'pending', data TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS request_app_date ON requests(app_id, started_at);
                CREATE TABLE IF NOT EXISTS attempts (
                    attempt_id TEXT PRIMARY KEY, request_id TEXT NOT NULL REFERENCES requests(request_id) ON DELETE CASCADE,
                    started_at TEXT NOT NULL, outcome TEXT NOT NULL DEFAULT 'submitted', data TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS attempt_request ON attempts(request_id);
            """)
            db.commit()
            self._initialized = True
        if time.monotonic() - self._last_prune >= 86400 or not self._last_prune:
            cutoff = (datetime.now(UTC) - timedelta(days=self.retention_days)).isoformat()
            db.execute("DELETE FROM requests WHERE started_at < ?", (cutoff,))
            db.commit()
            self._last_prune = time.monotonic()

    async def _run(self, operation):
        def run():
            if not self._lock.acquire(timeout=0.15):
                raise OSError("Usage store is busy")
            try:
                db = self._connect()
                try:
                    with db:
                        return operation(db)
                finally:
                    db.close()
            finally:
                self._lock.release()

        with anyio.fail_after(0.5):
            return await anyio.to_thread.run_sync(run, abandon_on_cancel=True)

    async def begin(self, record: dict) -> None:
        safe = {k: v for k, v in record.items() if k in REQUEST_FIELDS}
        await self._run(
            lambda db: db.execute(
                "INSERT INTO requests(request_id, app_id, started_at, data) VALUES(?,?,?,?)",
                (safe["request_id"], safe["app_id"], now(), json.dumps(safe, ensure_ascii=False)),
            )
        )

    async def start_attempt(self, request_id: str, record: dict) -> str:
        attempt_id = str(uuid.uuid4())
        safe = {k: v for k, v in record.items() if k in ATTEMPT_FIELDS}
        await self._run(
            lambda db: db.execute(
                "INSERT INTO attempts(attempt_id, request_id, started_at, data) VALUES(?,?,?,?)",
                (attempt_id, request_id, now(), json.dumps(safe, ensure_ascii=False)),
            )
        )
        return attempt_id

    async def _finish_row(self, table: str, key: str, record_id: str, record: dict, allowed: frozenset) -> None:
        safe = {k: v for k, v in record.items() if k in allowed}

        def update(db):
            row = db.execute(f"SELECT data FROM {table} WHERE {key}=?", (record_id,)).fetchone()
            if row is None:
                raise ValueError("Usage record not found")
            data = {**json.loads(row[0]), **safe}
            db.execute(
                f"UPDATE {table} SET outcome=?, data=? WHERE {key}=?",
                (data["outcome"], json.dumps(data, ensure_ascii=False), record_id),
            )

        await self._run(update)

    async def finish_attempt(self, attempt_id: str, record: dict) -> None:
        await self._finish_row("attempts", "attempt_id", attempt_id, record, ATTEMPT_FIELDS)

    async def finish(self, request_id: str, record: dict) -> None:
        await self._finish_row("requests", "request_id", request_id, record, REQUEST_FIELDS)

    async def summary(self, app_id: str, *, since: str | None = None, until: str | None = None) -> dict:
        def query(db):
            where, args = "app_id=?", [app_id]
            if since:
                where += " AND started_at>=?"
                args.append(since)
            if until:
                where += " AND started_at<?"
                args.append(until)
            requests = db.execute(f"SELECT data,outcome FROM requests WHERE {where}", args).fetchall()
            attempts = db.execute(
                f"SELECT a.data,a.outcome FROM attempts a JOIN requests r USING(request_id) WHERE {where.replace('started_at', 'r.started_at')}",
                args,
            ).fetchall()
            return requests, attempts

        requests, attempts = await self._run(query)
        records = [dict(json.loads(row), outcome=outcome) for row, outcome in requests]
        upstream = [dict(json.loads(row), outcome=outcome) for row, outcome in attempts]
        uncertain = sum(r["outcome"] in {"submitted", "timeout", "cancelled", "transport_error"} for r in upstream)
        unpriced = sum(
            r["outcome"] == "response" and r.get("http_status") == 200 and r.get("estimated_cost_cny") is None
            for r in upstream
        )
        cost = sum(
            (Decimal(r["estimated_cost_cny"]) for r in upstream if r.get("estimated_cost_cny") is not None),
            Decimal("0"),
        )
        groups = {}
        for record in records:
            group = groups.setdefault(
                (record.get("scene", "unspecified"), record.get("model") or record.get("requested_model")),
                {"requests": 0, "generated_images": 0, "cache_hits": 0},
            )
            group["requests"] += 1
            group["cache_hits"] += record["outcome"] == "cached"
            group["generated_images"] += bool(record.get("generation_id") and record["outcome"] != "cached")
        return {
            "app_id": app_id,
            "since": since,
            "until": until,
            "requests": len(records),
            "generated_images": sum(bool(r.get("generation_id") and r["outcome"] != "cached") for r in records),
            "cache_hits": sum(r["outcome"] == "cached" for r in records),
            "successful_requests": sum(r["outcome"] in {"success", "cached"} for r in records),
            "errors": sum(r["outcome"] == "error" for r in records),
            "cancelled": sum(r["outcome"] == "cancelled" for r in records),
            "incomplete_requests": sum(r["outcome"] == "pending" for r in records),
            "upstream_attempts": len(upstream),
            "potentially_billable_attempts": uncertain,
            "unpriced_response_attempts": unpriced,
            "estimate_complete": not uncertain and not unpriced,
            "estimated_cost_cny": format(cost, ".2f"),
            "cost_kind": "list_price_estimate",
            "breakdown": [{"scene": scene, "model": model, **counts} for (scene, model), counts in groups.items()],
        }

    async def ping(self) -> bool:
        return await self._run(lambda db: db.execute("SELECT 1").fetchone()[0]) == 1


@lru_cache(maxsize=32)
def usage_store(path: str, retention_days: int = 90) -> UsageStore:
    return UsageStore(path, retention_days)


def ark_estimate(payload: dict, response: dict) -> dict:
    """仅估算已知方舟出图价格；未知供应商或缺失像素不猜测为零。"""
    model = str(payload.get("model", ""))
    images = response.get("data")
    if not isinstance(images, list) or not images:
        return {}
    refs = payload.get("image")
    inputs = len(refs) if isinstance(refs, list) else int(bool(refs))
    total = Decimal("0")
    for image in images:
        if not isinstance(image, dict) or not (image.get("b64_json") or image.get("url")):
            return {}
        if "5-0-flash" in model:
            price = Decimal(".12")
        elif "5-0-lite" in model or model == "doubao-seedream-5-0-260128":
            price = Decimal(".22")
        elif "5-0-pro" in model:
            size = image.get("size") or payload.get("size")
            try:
                w, h = map(int, size.split("x"))
                if w <= 0 or h <= 0:
                    return {}
            except (AttributeError, TypeError, ValueError):
                return {}
            price = Decimal(".30") if w * h <= 2_610_000 else Decimal(".60")
        elif "4-5" in model:
            price = Decimal(".25")
        elif "4-0" in model:
            price = Decimal(".20")
        else:
            return {}
        total += price
    if "5-0-pro" in model:
        total += Decimal(".02") * max(0, inputs - 1)
    return {
        "estimated_cost_cny": str(total),
        "price_version": "ark-2026-10-23",
        "input_images": inputs,
        "generated_images": len(images),
        "output_size": images[0].get("size") or payload.get("size"),
    }
