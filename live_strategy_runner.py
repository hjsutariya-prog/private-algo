"""Paper-only BANKNIFTY strategy producer for one Tradetron bridge.

The runner consumes only complete observed stream candles and validated live
quotes.  It owns the portal decision; Tradetron remains a paper executor.  A
Tradetron acknowledgement is intentionally *not* treated as a fill, so every
sent event waits for an operator confirmation before the next position action.
That fail-closed rule prevents a missing remote execution from turning into an
invented local position.
"""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from typing import Any, Mapping

from engine import IST, strict_signal


RUNNER_CONFIRMATION = "start-paper-strategy-runner"
_DIRECTIONS = ("CE", "PE")


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _stamp(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, str):
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        raise ValueError("A stream time is required.")
    if result.tzinfo is None:
        raise ValueError("A timezone-aware stream time is required.")
    return result.astimezone(IST)


class StrategyRunnerStore:
    """Small durable state store.  It contains decisions, never credentials."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as db, db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS strategy_runner_state (
                    direction TEXT PRIMARY KEY,
                    payload TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS strategy_runner_hours (
                    hour_end TEXT PRIMARY KEY,
                    outcome TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    recorded_at TEXT NOT NULL
                );
            """)

    def _connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    @staticmethod
    def default(direction: str) -> dict[str, Any]:
        return {"direction": direction, "phase": "flat", "pending": None, "position": None,
                "last_action": None}

    def state(self, direction: str) -> dict[str, Any]:
        if direction not in _DIRECTIONS:
            raise ValueError("Direction must be CE or PE.")
        with closing(self._connect()) as db:
            row = db.execute("SELECT payload FROM strategy_runner_state WHERE direction=?", (direction,)).fetchone()
        if row is None:
            return self.default(direction)
        value = json.loads(row["payload"])
        if not isinstance(value, dict) or value.get("direction") != direction:
            raise ValueError("Saved strategy-runner state is invalid. Review this bridge before continuing.")
        return value

    def save(self, state: Mapping[str, Any]) -> dict[str, Any]:
        direction = state.get("direction")
        if direction not in _DIRECTIONS:
            raise ValueError("Strategy-runner state has an invalid direction.")
        value = dict(state)
        encoded = json.dumps(value, allow_nan=False, sort_keys=True, separators=(",", ":"))
        with closing(self._connect()) as db, db:
            db.execute("""
                INSERT INTO strategy_runner_state(direction,payload,updated_at) VALUES(?,?,?)
                ON CONFLICT(direction) DO UPDATE SET payload=excluded.payload,updated_at=excluded.updated_at
            """, (direction, encoded, _now()))
        return value

    def hour_recorded(self, hour_end: str) -> bool:
        with closing(self._connect()) as db:
            return db.execute("SELECT 1 FROM strategy_runner_hours WHERE hour_end=?", (hour_end,)).fetchone() is not None

    def record_hour(self, hour_end: str, outcome: str, detail: str) -> None:
        with closing(self._connect()) as db, db:
            db.execute("""
                INSERT OR IGNORE INTO strategy_runner_hours(hour_end,outcome,detail,recorded_at)
                VALUES(?,?,?,?)
            """, (hour_end, outcome, detail, _now()))

    def recent_hours(self, limit: int = 12) -> list[dict[str, str]]:
        with closing(self._connect()) as db:
            rows = db.execute("""
                SELECT hour_end,outcome,detail,recorded_at FROM strategy_runner_hours
                ORDER BY hour_end DESC LIMIT ?
            """, (max(1, min(limit, 100)),)).fetchall()
        return [dict(row) for row in rows]


class PaperStrategyRunner:
    """Produces exactly one-lot CE/PE events for one saved deployment.

    ``market`` is an adapter supplied by :class:`paper_runtime.PaperRuntime`.
    It deliberately exposes selection results and observed candles, not a
    broker order route.
    """

    def __init__(self, deployment_id: str, signal_runtime, path: str | Path):
        self.deployment_id = deployment_id
        self.signal_runtime = signal_runtime
        self.store = StrategyRunnerStore(path)
        self.enabled = False  # Never survives a local-process restart.
        self.blocked_reason: str | None = None
        self.message = "Automatic paper signals are off."
        # A position or in-flight remote event cannot be reconstructed from an
        # API acknowledgement.  Make restart recovery explicit rather than
        # re-entering or closing a position based on stale local assumptions.
        self.restart_review = any(self.store.state(direction)["phase"] != "flat" for direction in _DIRECTIONS)
        if self.restart_review:
            self.blocked_reason = "The app restarted with a recorded Tradetron position or pending signal. Review Tradetron before resuming."
            self.message = self.blocked_reason

    def status(self) -> dict[str, Any]:
        states = [self.store.state(direction) for direction in _DIRECTIONS]
        pending = [state["pending"] for state in states if state.get("pending")]
        if self.blocked_reason:
            state = "blocked"
        elif not self.enabled:
            state = "off"
        elif pending:
            state = "waiting_for_confirmation"
        else:
            state = "running"
        return {
            "mode": "paper_only",
            "automatic_runner": state,
            "enabled": self.enabled,
            "blocked": bool(self.blocked_reason),
            "message": self.message,
            "requires_operator_confirmation": True,
            "directions": [{"direction": item["direction"], "phase": item["phase"],
                            "pending_event": item.get("pending", {}).get("event_id") if item.get("pending") else None,
                            "position": item.get("position")} for item in states],
            "recent_hours": self.store.recent_hours(),
        }

    def start(self, confirmation: str) -> dict[str, Any]:
        if confirmation != RUNNER_CONFIRMATION:
            raise ValueError("Starting automatic paper signals requires its exact local confirmation.")
        if self.restart_review:
            raise ValueError("Review the recorded Tradetron state after restart before starting this runner.")
        signal_status = self.signal_runtime.status()
        if signal_status.get("blocked"):
            raise ValueError("Tradetron signal delivery is blocked. Review its last event before starting.")
        if not signal_status.get("armed"):
            raise ValueError("Arm the paper signal sender before starting the strategy runner.")
        self.enabled, self.blocked_reason = True, None
        self.message = "Waiting for complete 09:15-anchored BANKNIFTY hours and fresh option quotes."
        return self.status()

    def pause(self, note: str = "Automatic paper signals were paused.") -> dict[str, Any]:
        self.enabled = False
        self.message = note
        return self.status()

    def _block(self, reason: str) -> None:
        self.enabled = False
        self.blocked_reason = reason
        self.message = reason

    @staticmethod
    def _terminal(direction: str, position: Mapping[str, Any], spot: float) -> str | None:
        stop, target = float(position["stop_spot"]), float(position["target_spot"])
        if direction == "CE":
            return "stop" if spot >= stop else "target" if spot <= target else None
        return "stop" if spot <= stop else "target" if spot >= target else None

    @staticmethod
    def _hedge_triggered(direction: str, position: Mapping[str, Any], spot: float) -> bool:
        trigger = position.get("hedge_trigger_spot")
        if not isinstance(trigger, (int, float)):
            return False
        return spot >= trigger if direction == "CE" else spot <= trigger

    def _event_id(self, direction: str, event_type: str, sequence: int, at: datetime) -> str:
        compact = at.strftime("%Y%m%dT%H%M")
        return f"portal-{self.deployment_id[:12]}-{direction.lower()}-{event_type}-{compact}-{sequence:03d}"

    def _deliver(self, state: dict[str, Any], payload: dict[str, Any], pending: dict[str, Any]) -> None:
        signal_status = self.signal_runtime.status()
        if signal_status.get("pending_events"):
            self._block("A previous Tradetron signal is still queued. It was not replaced automatically.")
            return
        try:
            queued = self.signal_runtime.queue(payload)
            delivered = self.signal_runtime.dispatch_next()
        except ValueError as exc:
            self._block(str(exc))
            return
        if not delivered or delivered.get("event_id") != queued["event_id"]:
            self._block("Tradetron delivery did not return the expected event. No later signal was sent.")
            return
        if delivered.get("status") != "accepted_unconfirmed":
            self._block(delivered.get("note") or "Tradetron delivery was not confirmed. No later signal was sent.")
            return
        state["phase"] = "pending_" + payload["event_type"]
        state["pending"] = {**pending, "event_id": queued["event_id"], "event_type": payload["event_type"],
                            "at": _now()}
        state["last_action"] = {"event_id": queued["event_id"], "event_type": payload["event_type"], "status": "accepted_unconfirmed"}
        self.store.save(state)
        self.message = "Tradetron accepted a paper runtime update. Confirm its matching paper execution before the next action."

    def _exit(self, state: dict[str, Any], at: datetime, reason: str) -> None:
        position = state["position"]
        sequence = int(position["exit_sequence"]) + 1
        payload = {
            "event_id": self._event_id(state["direction"], "exit", sequence, at),
            "event_type": "exit", "direction": state["direction"], "sequence": sequence,
            "expiry": position["short_expiry"], "expiry_offset": int(position["short_expiry_offset"]),
            "quantity": int(position["quantity"]),
        }
        self._deliver(state, payload, {"reason": reason, "position": position, "exit_sequence": sequence})

    def _replace(self, market, state: dict[str, Any], at: datetime, *, reason: str, rollover: bool) -> None:
        position = state["position"]
        try:
            chosen = market.strategy_replacement_selection(state["direction"], at, position, rollover=rollover)
        except ValueError as exc:
            self._block(str(exc))
            return
        sequence = int(position["hedge_sequence"]) + 1
        payload = {
            "event_id": self._event_id(state["direction"], "hedge", sequence, at),
            "event_type": "hedge_replacement", "direction": state["direction"], "sequence": sequence,
            "expiry": chosen["expiry"], "expiry_offset": int(chosen["expiry_offset"]),
            "quantity": int(position["quantity"]),
            "hedge_symbol": chosen["hedge_symbol"], "hedge_strike": int(chosen["hedge_strike"]),
            "previous_hedge_symbol": position["hedge_symbol"], "previous_hedge_strike": int(position["hedge_strike"]),
            "previous_expiry": position["hedge_expiry"], "previous_expiry_offset": int(position["hedge_expiry_offset"]),
        }
        updated = {**position, "hedge_symbol": chosen["hedge_symbol"], "hedge_strike": int(chosen["hedge_strike"]),
                   "hedge_expiry": chosen["expiry"], "hedge_expiry_offset": int(chosen["expiry_offset"]),
                   "hedge_reference_spot": float(chosen["spot"]),
                   "hedge_trigger_spot": float(chosen["hedge_trigger_spot"]), "hedge_sequence": sequence}
        self._deliver(state, payload, {"reason": reason, "position": updated, "hedge_sequence": sequence})

    def _manage_direction(self, market, state: dict[str, Any], at: datetime, spot: float) -> None:
        if state["phase"] != "active":
            return
        position = state["position"]
        terminal = self._terminal(state["direction"], position, spot)
        # At 15:15, an opening gap exit is evaluated before a scheduled roll.
        if terminal:
            self._exit(state, at, f"spot_{terminal}")
            return
        try:
            rollover_at = market.calendar.rollover(datetime.fromisoformat(position["short_expiry"]).date())
        except ValueError as exc:
            self._block(str(exc))
            return
        # The scheduled roll replaces the old hedge before checking that old
        # hedge's own target.  Later ticks in the same minute then monitor the
        # confirmed new hedge normally.
        if position["hedge_expiry"] == position["short_expiry"] and at >= rollover_at:
            self._replace(market, state, at, reason="scheduled_rollover", rollover=True)
            return
        if self._hedge_triggered(state["direction"], position, spot):
            self._replace(market, state, at, reason="hedge_target", rollover=False)

    def _entry(self, market, direction: str, hour: Mapping[str, Any], at: datetime, spot: float) -> None:
        state = self.store.state(direction)
        if state["phase"] != "flat":
            self.store.record_hour(hour["end"], "skipped", f"{direction} already has an active or pending position.")
            return
        reference = float(hour["close"])
        stop = reference + 500 if direction == "CE" else reference - 500
        target = reference - 1000 if direction == "CE" else reference + 1000
        # The named close-boundary policy never enters after price has already
        # crossed the fixed risk level calculated from this completed candle.
        if self._terminal(direction, {"stop_spot": stop, "target_spot": target}, spot):
            self.store.record_hour(hour["end"], "skipped", "Available entry price was already beyond the fixed stop or target.")
            return
        try:
            chosen = market.strategy_entry_selection(direction, at)
        except ValueError as exc:
            self.store.record_hour(hour["end"], "blocked", str(exc))
            self._block(str(exc))
            return
        sequence = 1
        if state.get("last_action"):
            sequence = int(state["last_action"].get("entry_sequence", 0)) + 1
        position = {
            "entry_sequence": sequence, "exit_sequence": 0, "hedge_sequence": 0,
            "direction": direction, "quantity": int(chosen["quantity"]),
            "short_symbol": chosen["short_symbol"], "short_strike": int(chosen["short_strike"]),
            "short_expiry": chosen["expiry"], "short_expiry_offset": int(chosen["expiry_offset"]),
            "hedge_symbol": chosen["hedge_symbol"], "hedge_strike": int(chosen["hedge_strike"]),
            "hedge_expiry": chosen["expiry"], "hedge_expiry_offset": int(chosen["expiry_offset"]),
            "signal_hour_close": reference, "stop_spot": stop, "target_spot": target,
            "hedge_reference_spot": float(chosen["spot"]), "hedge_trigger_spot": float(chosen["hedge_trigger_spot"]),
        }
        payload = {
            "event_id": self._event_id(direction, "entry", sequence, at), "event_type": "entry",
            "direction": direction, "sequence": sequence, "expiry": chosen["expiry"],
            "expiry_offset": int(chosen["expiry_offset"]), "quantity": int(chosen["quantity"]),
            "short_symbol": chosen["short_symbol"], "short_strike": int(chosen["short_strike"]),
            "hedge_symbol": chosen["hedge_symbol"], "hedge_strike": int(chosen["hedge_strike"]),
            "signal_hour_close": reference, "stop_spot": stop, "target_spot": target,
        }
        self._deliver(state, payload, {"reason": "hourly_signal", "position": position, "entry_sequence": sequence})
        pending = state.get("pending") or {}
        if pending.get("event_id") == payload["event_id"]:
            self.store.record_hour(hour["end"], "entry_sent", f"{direction} entry accepted by Tradetron; execution confirmation required.")
        else:
            self.store.record_hour(hour["end"], "blocked", "The entry signal was not accepted by Tradetron. No position was assumed.")

    def _hourly_signal(self, market, at: datetime, spot: float) -> None:
        minute = at.replace(second=0, microsecond=0)
        # 09:15-10:15 through 14:15-15:15 are the only full regular hours.
        if minute.minute != 15 or minute.hour not in range(10, 16):
            return
        start = minute.replace(hour=minute.hour - 1)
        try:
            hour = market.stream.candles.hour(start, at)
        except ValueError:
            return  # Keep waiting within this boundary minute; never fill gaps.
        if self.store.hour_recorded(hour["end"]):
            return
        signal = strict_signal(hour["open"], hour["high"], hour["low"], hour["close"])
        if not signal:
            self.store.record_hour(hour["end"], "no_signal", "No strict 1,000-point crossing in the completed hour.")
            return
        self._entry(market, "PE" if signal["side"] == "bull" else "CE", hour, at, spot)

    def observe(self, market, quote: Mapping[str, Any]) -> dict[str, Any]:
        """Handle one fresh BANKNIFTY stream quote without inventing a fill."""
        if not self.enabled or self.blocked_reason:
            return self.status()
        if not self.signal_runtime.status().get("armed"):
            self.pause("Automatic paper signals paused because the Tradetron sender is disarmed.")
            return self.status()
        try:
            at, spot = _stamp(quote["timestamp"]), float(quote["last_price"])
            if not market.calendar.is_open(at):
                return self.status()
            # Existing position management is deliberately first: at 15:15 an
            # opening gap exit precedes the scheduled rollover.
            for direction in _DIRECTIONS:
                self._manage_direction(market, self.store.state(direction), at, spot)
                if self.blocked_reason:
                    return self.status()
            self._hourly_signal(market, at, spot)
        except (KeyError, TypeError, ValueError) as exc:
            self._block(str(exc))
        return self.status()

    def confirm(self, event_id: str) -> dict[str, Any]:
        """Record an operator's matching Tradetron paper-execution review."""
        if not isinstance(event_id, str) or not event_id:
            raise ValueError("Choose one Tradetron event to confirm.")
        confirmed = self.signal_runtime.confirm_after_operator_review(event_id)
        for direction in _DIRECTIONS:
            state = self.store.state(direction)
            pending = state.get("pending") or {}
            if pending.get("event_id") != event_id:
                continue
            event_type = pending.get("event_type")
            if event_type == "entry":
                state["phase"], state["position"] = "active", pending["position"]
            elif event_type == "exit":
                state["phase"], state["position"] = "flat", None
            elif event_type == "hedge_replacement":
                state["phase"], state["position"] = "active", pending["position"]
            else:
                raise ValueError("The saved pending strategy action is invalid.")
            state["pending"] = None
            state["last_action"] = {"event_id": event_id, "event_type": event_type,
                                    "status": confirmed["status"], "entry_sequence": pending.get("entry_sequence", state.get("last_action", {}).get("entry_sequence", 0))}
            self.store.save(state)
            self.message = "Tradetron paper execution was confirmed locally. The runner may monitor the next valid action."
            return self.status()
        raise ValueError("That Tradetron event is not waiting for strategy-runner confirmation.")
