from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from statistics import median

from .models import ObservationMetrics, ObservationResult, StockQuote


def connect_db(db_path: str | Path) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    init_db(conn)
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS quotes (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          symbol TEXT NOT NULL,
          code TEXT NOT NULL,
          name TEXT NOT NULL,
          provider TEXT NOT NULL,
          quote_time TEXT NOT NULL,
          current_price REAL NOT NULL,
          open_price REAL NOT NULL,
          previous_close REAL NOT NULL,
          high_price REAL NOT NULL,
          low_price REAL NOT NULL,
          change_amount REAL NOT NULL,
          change_percent REAL NOT NULL,
          volume_shares REAL NOT NULL,
          turnover_yuan REAL NOT NULL,
          raw_payload TEXT,
          created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE UNIQUE INDEX IF NOT EXISTS uq_quotes_symbol_time ON quotes(symbol, quote_time);
        CREATE INDEX IF NOT EXISTS idx_quotes_code_time ON quotes(code, quote_time);

        CREATE TABLE IF NOT EXISTS signals (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          quote_id INTEGER,
          symbol TEXT NOT NULL,
          code TEXT NOT NULL,
          signal_time TEXT NOT NULL,
          signal_level TEXT NOT NULL,
          should_notify INTEGER NOT NULL,
          observations_json TEXT NOT NULL,
          message TEXT NOT NULL,
          created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
          FOREIGN KEY (quote_id) REFERENCES quotes(id)
        );

        CREATE INDEX IF NOT EXISTS idx_signals_symbol_time ON signals(symbol, signal_time);
        CREATE INDEX IF NOT EXISTS idx_signals_level_time ON signals(signal_level, signal_time);

        CREATE TABLE IF NOT EXISTS signal_metrics (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          signal_id INTEGER NOT NULL,
          avg3 REAL,
          avg6 REAL,
          bias_to_avg3 REAL,
          bias_to_avg6 REAL,
          step_change_pct REAL,
          recent_range_pct REAL,
          intraday_amplitude_pct REAL,
          FOREIGN KEY (signal_id) REFERENCES signals(id)
        );
        """
    )
    conn.commit()


def insert_quote(conn: sqlite3.Connection, quote: StockQuote) -> int:
    conn.execute(
        """
        INSERT OR IGNORE INTO quotes (
          symbol, code, name, provider, quote_time, current_price, open_price,
          previous_close, high_price, low_price, change_amount, change_percent,
          volume_shares, turnover_yuan, raw_payload
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            quote.symbol,
            quote.code,
            quote.name,
            quote.provider,
            quote.quote_time.isoformat(sep=" "),
            float(quote.current_price),
            float(quote.open_price),
            float(quote.previous_close),
            float(quote.high_price),
            float(quote.low_price),
            float(quote.change_amount),
            float(quote.change_percent),
            float(quote.volume_shares),
            float(quote.turnover_yuan),
            quote.raw_payload,
        ),
    )
    row = conn.execute(
        "SELECT id FROM quotes WHERE symbol = ? AND quote_time = ?",
        (quote.symbol, quote.quote_time.isoformat(sep=" ")),
    ).fetchone()
    conn.commit()
    if row is None:
        raise RuntimeError("Failed to persist quote")
    return int(row["id"])


def insert_signal(conn: sqlite3.Connection, quote_id: int, quote: StockQuote, result: ObservationResult) -> int:
    cursor = conn.execute(
        """
        INSERT INTO signals (
          quote_id, symbol, code, signal_time, signal_level, should_notify, observations_json, message
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            quote_id,
            quote.symbol,
            quote.code,
            quote.quote_time.isoformat(sep=" "),
            result.signal_level,
            1 if result.should_notify else 0,
            json.dumps(result.observations, ensure_ascii=False),
            result.message,
        ),
    )
    signal_id = int(cursor.lastrowid)
    _insert_signal_metrics(conn, signal_id, result.metrics)
    conn.commit()
    return signal_id


def _insert_signal_metrics(conn: sqlite3.Connection, signal_id: int, metrics: ObservationMetrics) -> None:
    conn.execute(
        """
        INSERT INTO signal_metrics (
          signal_id, avg3, avg6, bias_to_avg3, bias_to_avg6, step_change_pct, recent_range_pct, intraday_amplitude_pct
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            signal_id,
            float(metrics.avg3),
            float(metrics.avg6),
            float(metrics.bias_to_avg3),
            float(metrics.bias_to_avg6),
            float(metrics.step_change_pct),
            float(metrics.recent_range_pct),
            float(metrics.intraday_amplitude_pct),
        ),
    )


def replay_signal_stats(
    conn: sqlite3.Connection,
    *,
    symbol: str | None = None,
    signal_level: str | None = None,
    horizons: tuple[int, ...] = (1, 3, 5),
) -> dict:
    clauses: list[str] = []
    params: list[object] = []
    if symbol:
        clauses.append("s.symbol = ?")
        params.append(symbol)
    if signal_level:
        clauses.append("s.signal_level = ?")
        params.append(signal_level)

    where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = conn.execute(
        f"""
        SELECT s.id AS signal_id, s.symbol, s.code, s.signal_time, s.signal_level, q.current_price
        FROM signals s
        JOIN quotes q ON q.id = s.quote_id
        {where_sql}
        ORDER BY s.symbol, s.signal_time
        """,
        params,
    ).fetchall()

    by_horizon: dict[int, list[float]] = {h: [] for h in horizons}
    signal_count = 0

    for row in rows:
        signal_count += 1
        future_quotes = conn.execute(
            """
            SELECT current_price
            FROM quotes
            WHERE symbol = ? AND quote_time > ?
            ORDER BY quote_time ASC
            LIMIT ?
            """,
            (row["symbol"], row["signal_time"], max(horizons)),
        ).fetchall()
        base_price = float(row["current_price"])
        if base_price <= 0:
            continue
        for horizon in horizons:
            if len(future_quotes) < horizon:
                continue
            future_price = float(future_quotes[horizon - 1]["current_price"])
            ret = ((future_price - base_price) / base_price) * 100
            by_horizon[horizon].append(ret)

    return {
        "generated_at": datetime.now().isoformat(sep=" ", timespec="seconds"),
        "signal_count": signal_count,
        "horizons": {str(h): _summarize_returns(values) for h, values in by_horizon.items()},
    }


def _summarize_returns(values: list[float]) -> dict:
    if not values:
        return {"samples": 0, "avg": None, "median": None, "win_rate": None, "min": None, "max": None}
    wins = sum(1 for v in values if v > 0)
    return {
        "samples": len(values),
        "avg": round(sum(values) / len(values), 4),
        "median": round(median(values), 4),
        "win_rate": round(wins / len(values) * 100, 2),
        "min": round(min(values), 4),
        "max": round(max(values), 4),
    }
