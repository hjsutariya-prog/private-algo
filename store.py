"""Local SQLite strategy drafts and offline-check history. No credentials."""
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import hashlib
import math
from pathlib import Path
import sqlite3
from uuid import uuid4

READINESS = ["Order execution service", "Streaming market data", "Verified exchange calendar", "Real-quote paper tests", "Live approval"]
RULES = {
    "signal": "Completed BANKNIFTY spot hourly candles anchored at 09:15 IST; strict 1,000-point crossings.",
    "short_selection": "Monthly ATM/ITM option with quoted premium closest to ₹1,000. Not a guaranteed fill.",
    "stop_target": "Fixed 500-point stop and 1,000-point target from the completed signal hour's spot close (10:15, 11:15, etc.), not the execution price. Keep this original reference through later hours and rollovers. Skip entry if its available price is already at or beyond either level. No trailing or daily exit.",
    "hedge": "Live plan only (not implemented): open independent CE/PE hedges Monday 15:15 in expiry week; buy replacement fully before selling the old hedge. Historical simulations use the separate model below.",
    "backtest_model": "Shared simulator, independent of this draft: recorded exact-contract option prices first, labelled premium estimates when missing. Fallback sold entry ₹1,000; independent CE/PE hedges from the first checked minute. Lower hedge premium floor(spot / 10,000) × ₹100. Historical replacements use completed-minute confirmation and next-minute reopening; live buy-first protection is a separate execution requirement. Charges are estimates, not actual fills or broker profit.",
    "rollover": "Monday of actual expiry week, 15:15 IST; first exchange session if Monday is a holiday. Keep original short stops and targets.",
    "sizing": "Base lots per side and per independent hedge, multiplied by the saved multiplier. One bullish and one bearish short may coexist. No compounding.",
    "capital_note": "Capital is a planning amount, not a verified broker margin or enforced allocation. All strategies share broker funds once connected.",
}


def timestamp():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS strategies (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, lots INTEGER NOT NULL,
                    multiplier INTEGER NOT NULL DEFAULT 1, capital REAL NOT NULL,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY, strategy_id TEXT NOT NULL, created_at TEXT NOT NULL,
                    payload TEXT NOT NULL, FOREIGN KEY(strategy_id) REFERENCES strategies(id)
                );
                CREATE TABLE IF NOT EXISTS backtests (
                    id TEXT PRIMARY KEY, created_at TEXT NOT NULL, summary TEXT NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS deployments (
                    id TEXT PRIMARY KEY, strategy_id TEXT NOT NULL,
                    strategy_version TEXT NOT NULL, strategy_snapshot TEXT NOT NULL,
                    mode TEXT NOT NULL, state TEXT NOT NULL,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    UNIQUE(strategy_id, strategy_version),
                    FOREIGN KEY(strategy_id) REFERENCES strategies(id)
                );
            """)
            columns = {row[1] for row in db.execute("PRAGMA table_info(strategies)")}
            if "multiplier" not in columns:
                db.execute("ALTER TABLE strategies ADD COLUMN multiplier INTEGER NOT NULL DEFAULT 1")
            if db.execute("SELECT COUNT(*) FROM strategies").fetchone()[0] == 0:
                now = timestamp()
                db.execute("INSERT INTO strategies (id,name,lots,multiplier,capital,created_at,updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                           (uuid4().hex, "BANKNIFTY · Hourly 1,000-point", 1, 1, 1000000, now, now))

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    def _view(self, row):
        result = dict(row)
        result["multiplier"] = result.get("multiplier", 1)
        result["effective_lots"] = result["lots"] * result["multiplier"]
        result.update(status="draft", rules=dict(RULES), readiness=[{"label": label, "ready": False} for label in READINESS])
        snapshot = {key: result[key] for key in ("id", "name", "lots", "multiplier", "effective_lots", "capital", "rules")}
        result["version"] = "draft-" + hashlib.sha256(json.dumps(snapshot, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:12]
        return result

    def list_strategies(self):
        with self.connect() as db:
            return [self._view(row) for row in db.execute("SELECT * FROM strategies ORDER BY created_at, rowid")]

    def get_strategy(self, strategy_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM strategies WHERE id=?", (strategy_id,)).fetchone()
        if row is None:
            raise KeyError("Strategy not found.")
        return self._view(row)

    @staticmethod
    def _deployment_view(row):
        result = dict(row)
        snapshot = json.loads(result.pop("strategy_snapshot"))
        result.update({
            "mode": "paper_only", "state": result["state"], "strategy": snapshot,
            "strategy_name": snapshot["name"], "lots": snapshot["lots"],
            "multiplier": snapshot["multiplier"], "effective_lots": snapshot["effective_lots"],
            "executor": {"linked": False, "status": "not_linked"},
            "runner": {"status": "not_connected"},
            "outbox": {"status": "not_created"},
            "note": "Paper deployment record only. It has no linked executor and cannot send a Tradetron signal.",
        })
        return result

    def list_deployments(self):
        with self.connect() as db:
            rows = db.execute("SELECT * FROM deployments ORDER BY created_at DESC, rowid DESC").fetchall()
        return [self._deployment_view(row) for row in rows]

    def get_deployment(self, deployment_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM deployments WHERE id=?", (deployment_id,)).fetchone()
        if row is None:
            raise KeyError("Deployment not found.")
        return self._deployment_view(row)

    def create_deployment(self, values):
        """Freeze one draft version as a paper-only deployment record.

        A deployment is deliberately not a Tradetron instruction. Linking an
        executor and starting a runner are later, separate gated actions.
        """
        if not isinstance(values, dict) or set(values) != {"strategy_id"} or not isinstance(values["strategy_id"], str):
            raise ValueError("Choose one saved strategy draft for the paper deployment.")
        strategy = self.get_strategy(values["strategy_id"])
        snapshot = {key: strategy[key] for key in ("id", "name", "lots", "multiplier", "effective_lots", "capital", "rules", "version")}
        identity, now = uuid4().hex, timestamp()
        try:
            with self.connect() as db:
                db.execute("""
                    INSERT INTO deployments (id,strategy_id,strategy_version,strategy_snapshot,mode,state,created_at,updated_at)
                    VALUES (?, ?, ?, ?, 'paper_only', 'not_linked', ?, ?)
                """, (identity, strategy["id"], strategy["version"],
                      json.dumps(snapshot, sort_keys=True, ensure_ascii=False, separators=(",", ":")), now, now))
        except sqlite3.IntegrityError as exc:
            raise ValueError("This exact strategy version already has a paper deployment. Edit the draft first to create a separate deployment version.") from exc
        return self.get_deployment(identity)

    def validate(self, values):
        if not isinstance(values, dict) or set(values) - {"name", "lots", "multiplier", "capital"}:
            raise ValueError("Only strategy name, base lots, multiplier and planning capital can be changed.")
        if not {"name", "lots", "capital"}.issubset(values):
            raise ValueError("Name, base lots and planning capital are required.")
        name, lots, multiplier, capital = values["name"], values["lots"], values.get("multiplier", 1), values["capital"]
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 80 or any(ord(c) < 32 for c in name):
            raise ValueError("Use a strategy name with 1–80 characters and no control characters.")
        if type(lots) is not int or not 1 <= lots <= 100:
            raise ValueError("Base lots must be a whole number from 1 to 100.")
        if type(multiplier) is not int or not 1 <= multiplier <= 100:
            raise ValueError("Multiplier must be a whole number from 1 to 100.")
        if type(capital) not in (int, float) or not math.isfinite(capital) or not 1 <= capital <= 1000000000:
            raise ValueError("Planning capital must be a number from ₹1 to ₹1,000,000,000.")
        return {"name": name.strip(), "lots": lots, "multiplier": multiplier, "capital": capital}

    def create_strategy(self, values):
        valid = self.validate(values)
        identity, now = uuid4().hex, timestamp()
        with self.connect() as db:
            db.execute("INSERT INTO strategies (id,name,lots,multiplier,capital,created_at,updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                       (identity, valid["name"], valid["lots"], valid["multiplier"], valid["capital"], now, now))
        return self.get_strategy(identity)

    def update_strategy(self, identity, values):
        current = self.get_strategy(identity)
        if not isinstance(values, dict) or not values or set(values) - {"name", "lots", "multiplier", "capital"}:
            raise ValueError("Only name, base lots, multiplier and planning capital can be changed.")
        merged = {key: values.get(key, current[key]) for key in ("name", "lots", "multiplier", "capital")}
        valid = self.validate(merged)
        with self.connect() as db:
            db.execute("UPDATE strategies SET name=?,lots=?,multiplier=?,capital=?,updated_at=? WHERE id=?",
                       (valid["name"], valid["lots"], valid["multiplier"], valid["capital"], timestamp(), identity))
        return self.get_strategy(identity)

    def add_run(self, strategy, result):
        payload = dict(result, id=uuid4().hex, created_at=timestamp(),
                       strategy_id=strategy["id"], strategy_name=strategy["name"],
                       strategy_version=strategy.get("version"),
                       strategy_snapshot={key: strategy[key] for key in ("name", "lots", "multiplier", "effective_lots", "capital", "rules") if key in strategy})
        with self.connect() as db:
            db.execute("INSERT INTO runs VALUES (?, ?, ?, ?)",
                       (payload["id"], strategy["id"], payload["created_at"], json.dumps(payload, allow_nan=False)))
        return payload

    def recent_runs(self):
        with self.connect() as db:
            return [json.loads(row[0]) for row in db.execute("SELECT payload FROM runs ORDER BY rowid DESC LIMIT 20")]

    def get_run(self, identity):
        with self.connect() as db:
            row = db.execute("SELECT payload FROM runs WHERE id=?", (identity,)).fetchone()
        if row is None:
            raise KeyError("Check history not found.")
        return json.loads(row[0])

    @staticmethod
    def _backtest_summary(result, purpose):
        if purpose not in {"research", "paper_replay"}:
            raise ValueError("Unknown backtest type.")
        if not isinstance(result, dict):
            raise ValueError("A completed backtest result is required.")
        checked = result.get("range")
        if not isinstance(checked, dict) or not all(isinstance(checked.get(key), str) for key in ("start_date", "end_date")):
            raise ValueError("The completed backtest has no valid date range.")
        spot = result.get("spot_proxy") if isinstance(result.get("spot_proxy"), dict) else {}
        options = result.get("option_model") if isinstance(result.get("option_model"), dict) else {}
        strategy = spot.get("strategy") if isinstance(spot.get("strategy"), dict) else {}
        combined = options.get("combined") if isinstance(options.get("combined"), dict) else {}
        total = combined.get("total_net")
        if isinstance(total, bool) or not isinstance(total, (int, float)) or not math.isfinite(total):
            total = None
        return {
            "purpose": purpose,
            "start_date": checked["start_date"],
            "end_date": checked["end_date"],
            "signals": checked.get("total_signals") if isinstance(checked.get("total_signals"), int) else None,
            "strategy_mode": strategy.get("id") if isinstance(strategy.get("id"), str) else None,
            "validation": options.get("validation_status") if isinstance(options.get("validation_status"), str) else spot.get("validation_status"),
            "approximate": bool(options.get("approximate") or spot.get("approximate") or options.get("approximate_due_to_missing_minutes") or options.get("approximate_due_to_assumed_holidays")),
            "combined_net": total,
        }

    def add_backtest(self, result, purpose="research"):
        summary = self._backtest_summary(result, purpose)
        payload = {"id": uuid4().hex, "created_at": timestamp(), "summary": summary, "result": result}
        encoded_summary = json.dumps(summary, allow_nan=False, separators=(",", ":"))
        encoded_payload = json.dumps(payload, allow_nan=False, separators=(",", ":"))
        with self.connect() as db:
            db.execute("INSERT INTO backtests VALUES (?, ?, ?, ?)",
                       (payload["id"], payload["created_at"], encoded_summary, encoded_payload))
            db.execute("DELETE FROM backtests WHERE id NOT IN (SELECT id FROM backtests ORDER BY rowid DESC LIMIT 20)")
        return {"id": payload["id"], "created_at": payload["created_at"], **summary}

    def recent_backtests(self):
        with self.connect() as db:
            rows = db.execute("SELECT id, created_at, summary FROM backtests ORDER BY rowid DESC LIMIT 20").fetchall()
        return [{"id": row["id"], "created_at": row["created_at"], **json.loads(row["summary"])} for row in rows]

    def get_backtest(self, identity):
        with self.connect() as db:
            row = db.execute("SELECT payload FROM backtests WHERE id=?", (identity,)).fetchone()
        if row is None:
            raise KeyError("Backtest history not found.")
        return json.loads(row["payload"])
