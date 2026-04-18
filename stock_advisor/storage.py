from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from statistics import median

from .models import DecisionSignal, ObservationMetrics, ObservationResult, StockQuote


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

        CREATE TABLE IF NOT EXISTS decision_signals (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          signal_id INTEGER NOT NULL,
          symbol TEXT NOT NULL,
          code TEXT NOT NULL,
          action TEXT NOT NULL,
          score REAL NOT NULL,
          confidence TEXT NOT NULL,
          regime TEXT NOT NULL,
          rationale_json TEXT NOT NULL,
          risk_flags_json TEXT NOT NULL,
          created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
          FOREIGN KEY (signal_id) REFERENCES signals(id)
        );

        CREATE INDEX IF NOT EXISTS idx_decisions_symbol_time ON decision_signals(symbol, created_at);
        CREATE INDEX IF NOT EXISTS idx_decisions_action_time ON decision_signals(action, created_at);
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
    _insert_decision_signal(conn, signal_id, quote, result.decision)
    return signal_id


def persist_observation(conn: sqlite3.Connection, quote: StockQuote, result: ObservationResult) -> tuple[int, int]:
    with conn:
        quote_id = insert_quote(conn, quote)
        signal_id = insert_signal(conn, quote_id, quote, result)
    return quote_id, signal_id


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


def _insert_decision_signal(conn: sqlite3.Connection, signal_id: int, quote: StockQuote, decision: DecisionSignal) -> None:
    conn.execute(
        """
        INSERT INTO decision_signals (
          signal_id, symbol, code, action, score, confidence, regime, rationale_json, risk_flags_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            signal_id,
            quote.symbol,
            quote.code,
            decision.action,
            float(decision.score),
            decision.confidence,
            decision.regime,
            json.dumps(decision.rationale, ensure_ascii=False),
            json.dumps(decision.risk_flags, ensure_ascii=False),
        ),
    )


def replay_signal_stats(
    conn: sqlite3.Connection,
    *,
    symbol: str | None = None,
    signal_level: str | None = None,
    action: str | None = None,
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
    if action:
        clauses.append("d.action = ?")
        params.append(action)

    where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = conn.execute(
        f"""
        SELECT s.id AS signal_id, s.symbol, s.code, s.signal_time, s.signal_level, q.current_price,
               d.action, d.score, d.confidence, d.regime
        FROM signals s
        JOIN quotes q ON q.id = s.quote_id
        LEFT JOIN decision_signals d ON d.signal_id = s.id
        {where_sql}
        ORDER BY s.symbol, s.signal_time
        """,
        params,
    ).fetchall()

    by_horizon: dict[int, list[float]] = {h: [] for h in horizons}
    signal_count = 0
    actions: dict[str, int] = {}
    scores: list[float] = []

    for row in rows:
        signal_count += 1
        action_name = row["action"] or "unknown"
        actions[action_name] = actions.get(action_name, 0) + 1
        if row["score"] is not None:
            scores.append(float(row["score"]))
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
        "action_breakdown": actions,
        "avg_score": round(sum(scores) / len(scores), 4) if scores else None,
        "horizons": {str(h): _summarize_returns(values) for h, values in by_horizon.items()},
    }


def load_recent_quotes(conn: sqlite3.Connection, symbol: str, limit: int) -> list[StockQuote]:
    rows = conn.execute(
        """
        SELECT provider, symbol, code, name, current_price, open_price, previous_close, high_price, low_price,
               change_amount, change_percent, volume_shares, turnover_yuan, quote_time, raw_payload
        FROM quotes
        WHERE symbol = ?
        ORDER BY quote_time DESC
        LIMIT ?
        """,
        (symbol, limit),
    ).fetchall()
    return [_row_to_quote(row) for row in reversed(rows)]


def fetch_latest_briefing(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """
        SELECT q.symbol, q.code, q.name, q.quote_time, q.current_price, q.change_percent,
               s.signal_level, d.action, d.score, d.confidence, d.regime, d.rationale_json, d.risk_flags_json
        FROM quotes q
        JOIN (
          SELECT symbol, MAX(quote_time) AS latest_time
          FROM quotes
          GROUP BY symbol
        ) latest ON latest.symbol = q.symbol AND latest.latest_time = q.quote_time
        LEFT JOIN (
          SELECT quote_id, MAX(id) AS latest_signal_id
          FROM signals
          GROUP BY quote_id
        ) latest_signal ON latest_signal.quote_id = q.id
        LEFT JOIN signals s ON s.id = latest_signal.latest_signal_id
        LEFT JOIN decision_signals d ON d.signal_id = s.id
        ORDER BY COALESCE(d.score, 0) DESC, q.symbol ASC
        """
    ).fetchall()
    return [
        {
            "symbol": row["symbol"],
            "code": row["code"],
            "name": row["name"],
            "quote_time": row["quote_time"],
            "current_price": round(float(row["current_price"]), 3),
            "change_percent": round(float(row["change_percent"]), 2),
            "signal_level": row["signal_level"] or "UNKNOWN",
            "action": row["action"] or "unknown",
            "score": round(float(row["score"]), 2) if row["score"] is not None else None,
            "confidence": row["confidence"] or "unknown",
            "regime": row["regime"] or "unknown",
            "rationale": json.loads(row["rationale_json"]) if row["rationale_json"] else [],
            "risk_flags": json.loads(row["risk_flags_json"]) if row["risk_flags_json"] else [],
        }
        for row in rows
    ]


def _row_to_quote(row: sqlite3.Row) -> StockQuote:
    return StockQuote(
        provider=row["provider"],
        symbol=row["symbol"],
        code=row["code"],
        name=row["name"],
        current_price=_decimal(row["current_price"]),
        open_price=_decimal(row["open_price"]),
        previous_close=_decimal(row["previous_close"]),
        high_price=_decimal(row["high_price"]),
        low_price=_decimal(row["low_price"]),
        change_amount=_decimal(row["change_amount"]),
        change_percent=_decimal(row["change_percent"]),
        volume_shares=_decimal(row["volume_shares"]),
        turnover_yuan=_decimal(row["turnover_yuan"]),
        quote_time=datetime.fromisoformat(row["quote_time"]),
        raw_payload=row["raw_payload"] or "",
    )


def _decimal(value: object) -> Decimal:
    return Decimal(str(value))


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
