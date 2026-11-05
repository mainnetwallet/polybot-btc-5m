"""Batched Parquet recorder with read-only DuckDB queries and retention.

Layout: ``<root>/<dataset>/date=YYYY-MM-DD/part-<utc>-<id>.parquet`` (zstd compressed).
Rows are buffered in memory and written in batches; raw files are never modified in place.
Only a fixed allow-list of dataset names can be written, so arbitrary data cannot be dumped
to disk through this interface.
"""
from __future__ import annotations

import logging
import os
import shutil
import threading
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

log = logging.getLogger(__name__)

_TS = pa.timestamp("us", tz="UTC")

# Explicit column types keep schemas stable across batches. Identifiers such as asset_id are
# very large integers in string form and must stay strings. Unknown columns are inferred.
_STR, _F64, _I64 = pa.string(), pa.float64(), pa.int64()
_COMMON = {"ts": _TS, "recv_ts": _TS}
_SCHEMAS: dict[str, dict[str, pa.DataType]] = {
    "btc_ticks": {**_COMMON, "source": _STR, "price": _F64, "qty": _F64, "sequence": _I64},
    "oracle_twap": {**_COMMON, "value": _F64, "source": _STR, "window_seconds": _I64, "symbol": _STR},
    "book_snapshots": {
        **_COMMON, "market_id": _STR, "asset_id": _STR, "outcome": _STR,
        "best_bid": _F64, "best_ask": _F64, "spread": _F64,
        "bid_depth": _F64, "ask_depth": _F64, "source": _STR,
    },
    "market_snapshots": {**_COMMON, "market_id": _STR},
    "signal_features": {
        **_COMMON, "btc_price": _F64, "return_5s": _F64, "return_15s": _F64, "return_60s": _F64,
        "vol_60s": _F64, "mtf_agreement": _F64, "twap": _F64, "projected_twap": _F64,
        "remaining_seconds": _F64, "implied_up": _F64, "implied_down": _F64,
    },
    "paper_fills": {**_COMMON, "market_id": _STR},
}
DATASETS = frozenset(_SCHEMAS)

DEFAULT_RETENTION_DAYS = 30
DEFAULT_FLUSH_SECONDS = 10.0
MAX_BUFFERED_ROWS = 50_000          # per dataset; oldest rows are dropped beyond this
AUTO_FLUSH_ROWS = 5_000
_RETENTION_CHECK_SECONDS = 6 * 3600


def _to_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str) and value:
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def _convert(value: Any, dtype: pa.DataType) -> Any:
    if value is None:
        return None
    try:
        if dtype == _TS:
            return _to_datetime(value)
        if dtype == _F64:
            number = float(value)
            return number if number == number and number not in (float("inf"), float("-inf")) else None
        if dtype == _I64:
            return int(value)
        return str(value)
    except (TypeError, ValueError, ArithmeticError):
        return None


def _infer(values: Iterable[Any]) -> pa.DataType:
    kinds = {type(v) for v in values if v is not None}
    if not kinds:
        return _STR
    if kinds <= {bool}:
        return pa.bool_()
    if kinds <= {int}:
        return _I64
    if kinds <= {int, float, Decimal}:
        return _F64
    if kinds <= {datetime}:
        return _TS
    return _STR


class ParquetRecorder:
    def __init__(
        self,
        root: Path | str,
        *,
        retention_days: int | None = DEFAULT_RETENTION_DAYS,
        flush_interval_seconds: float = DEFAULT_FLUSH_SECONDS,
    ) -> None:
        self.root = Path(root)
        self.retention_days = retention_days
        self.flush_interval_seconds = flush_interval_seconds
        self._buffers: dict[str, list[dict[str, Any]]] = {}
        self._lock = threading.RLock()
        self._last_flush = time.monotonic()
        self._last_retention = float("-inf")
        self.root.mkdir(parents=True, exist_ok=True)
        self.enforce_retention()

    # ------------------------------------------------------------------ writing
    def write(self, dataset: str, row: dict[str, Any]) -> None:
        self.write_many(dataset, [row])

    def write_many(self, dataset: str, rows: Iterable[dict[str, Any]]) -> None:
        if dataset not in DATASETS:
            raise ValueError(f"unknown dataset {dataset!r}; allowed: {sorted(DATASETS)}")
        with self._lock:
            buffer = self._buffers.setdefault(dataset, [])
            buffer.extend(dict(r) for r in rows)
            if len(buffer) > MAX_BUFFERED_ROWS:
                dropped = len(buffer) - MAX_BUFFERED_ROWS
                del buffer[:dropped]
                log.error("recorder buffer for %s overflowed; dropped %d oldest rows", dataset, dropped)
            due = (
                sum(len(b) for b in self._buffers.values()) >= AUTO_FLUSH_ROWS
                or time.monotonic() - self._last_flush >= self.flush_interval_seconds
            )
        if due:
            try:
                self.flush()
            except Exception:  # noqa: BLE001 - recording must never take the engine down
                log.exception("parquet auto-flush failed; rows stay buffered")

    def flush(self) -> list[Path]:
        """Write every buffered row to Parquet. Returns the files written."""
        with self._lock:
            pending, self._buffers = self._buffers, {}
            self._last_flush = time.monotonic()
        written: list[Path] = []
        failed: dict[str, list[dict[str, Any]]] = {}
        error: Exception | None = None
        for dataset, rows in pending.items():
            if not rows:
                continue
            try:
                written.extend(self._write_dataset(dataset, rows))
            except Exception as exc:  # noqa: BLE001
                failed[dataset] = rows
                error = exc
        if failed:
            with self._lock:  # put unwritten rows back (ahead of anything newer) and report
                for dataset, rows in failed.items():
                    self._buffers[dataset] = rows + self._buffers.get(dataset, [])
            raise error  # type: ignore[misc]
        self._maybe_enforce_retention()
        return written

    def close(self) -> None:
        self.flush()

    def _write_dataset(self, dataset: str, rows: list[dict[str, Any]]) -> list[Path]:
        by_day: dict[date, list[dict[str, Any]]] = {}
        now = datetime.now(timezone.utc)
        for row in rows:
            day = (_to_datetime(row.get("ts")) or now).date()
            by_day.setdefault(day, []).append(row)
        files: list[Path] = []
        schema = self._schema(dataset, rows)
        stamp = now.strftime("%Y%m%dT%H%M%S")
        for day, day_rows in sorted(by_day.items()):
            target_dir = self.root / dataset / f"date={day.isoformat()}"
            target_dir.mkdir(parents=True, exist_ok=True)
            table = pa.Table.from_arrays(
                [pa.array([_convert(r.get(f.name), f.type) for r in day_rows], type=f.type) for f in schema],
                schema=schema,
            )
            final = target_dir / f"part-{stamp}-{uuid.uuid4().hex[:8]}.parquet"
            tmp = final.with_name(final.name + ".tmp")
            pq.write_table(table, tmp, compression="zstd")
            os.replace(tmp, final)
            files.append(final)
        return files

    @staticmethod
    def _schema(dataset: str, rows: list[dict[str, Any]]) -> pa.Schema:
        known = _SCHEMAS[dataset]
        fields = [pa.field(name, dtype) for name, dtype in known.items()]
        extras = sorted({k for r in rows for k in r} - set(known))
        for name in extras:
            fields.append(pa.field(name, _infer(r.get(name) for r in rows)))
        return pa.schema(fields)

    # ----------------------------------------------------------------- querying
    def query(self, dataset: str, sql: str) -> list[tuple]:
        """Run a single read-only SELECT against a dataset, exposed as a view named ``dataset``."""
        if dataset not in DATASETS:
            raise ValueError(f"unknown dataset {dataset!r}; allowed: {sorted(DATASETS)}")
        self.flush()
        con = duckdb.connect(":memory:")
        try:
            statements = con.extract_statements(sql)
            if len(statements) != 1 or statements[0].type != duckdb.StatementType.SELECT:
                raise ValueError("only a single read-only SELECT statement is allowed")
            folder = self.root / dataset
            if any(folder.glob("date=*/*.parquet")):
                pattern = (folder / "date=*" / "*.parquet").as_posix()
                con.execute(
                    f'CREATE VIEW "{dataset}" AS SELECT * FROM '
                    f"read_parquet('{pattern}', hive_partitioning=true, union_by_name=true)"
                )
            else:
                con.register(dataset, pa.schema([pa.field(n, t) for n, t in _SCHEMAS[dataset].items()]).empty_table())
            try:
                con.execute(f"SET allowed_directories=['{folder.resolve().as_posix()}']")
                con.execute("SET enable_external_access=false")
            except duckdb.Error:
                log.warning("DuckDB could not lock down file access; relying on SELECT-only validation")
            return con.execute(sql).fetchall()
        finally:
            con.close()

    # ---------------------------------------------------------------- retention
    def enforce_retention(self, today: date | None = None) -> int:
        """Delete date partitions older than ``retention_days``. Returns partitions removed."""
        self._last_retention = time.monotonic()
        if not self.retention_days or self.retention_days <= 0:
            return 0
        cutoff = (today or datetime.now(timezone.utc).date()) - timedelta(days=self.retention_days)
        removed = 0
        for dataset in DATASETS:
            base = self.root / dataset
            if not base.is_dir():
                continue
            for part in base.glob("date=*"):
                try:
                    part_day = date.fromisoformat(part.name.split("=", 1)[1])
                except ValueError:
                    continue
                if part_day < cutoff and part.is_dir():
                    shutil.rmtree(part, ignore_errors=True)
                    removed += 1
        if removed:
            log.info("parquet retention removed %d partition(s) older than %s", removed, cutoff)
        return removed

    def _maybe_enforce_retention(self) -> None:
        if time.monotonic() - self._last_retention >= _RETENTION_CHECK_SECONDS:
            try:
                self.enforce_retention()
            except Exception:  # noqa: BLE001
                log.exception("parquet retention failed")
