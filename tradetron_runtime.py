"""Durable, deliberately disarmed paper signal outbox for Tradetron.

The portal has to make the BANKNIFTY decision before anything appears here.
This module stores that already-validated decision, prevents accidental
duplicates, and records the important distinction between a Tradetron API
acknowledgement and a confirmed trade.  It has no broker API and no automatic
retry after an uncertain request, because retrying could duplicate a signal.
"""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any, Callable, Mapping

from tradetron_delivery import TradetronDeliveryError, TradetronPaperDelivery
from tradetron_protocol import PortalSignal, parse_portal_signal


OUTBOX_STATUSES = frozenset(("queued", "sending", "accepted_unconfirmed", "uncertain", "operator_confirmed", "blocked"))
ARMING_CONFIRMATION = "paper-signal-runner"


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _json(signal: PortalSignal) -> str:
    return json.dumps(asdict(signal), sort_keys=True, separators=(",", ":"))


def _fingerprint(payload: str) -> str:
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class TradetronSignalStore:
    """SQLite record of outbound paper executor signals.

    The storage deliberately retains no API token or URL.  It has only the
    validated event that the portal decided and an outcome state.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialise()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialise(self) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute("""
                CREATE TABLE IF NOT EXISTS tradetron_signal_outbox (
                    event_id TEXT PRIMARY KEY,
                    event_type TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    expiry TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    fingerprint TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    sent_at TEXT,
                    note TEXT,
                    UNIQUE(event_type, direction, sequence)
                )
            """)

    @staticmethod
    def _visible(row: sqlite3.Row) -> dict[str, Any]:
        payload = json.loads(row["payload"])
        return {
            "event_id": row["event_id"],
            "event_type": row["event_type"],
            "direction": row["direction"],
            "sequence": row["sequence"],
            "expiry": row["expiry"],
            "quantity": payload["quantity"],
            "status": row["status"],
            "created_at": row["created_at"],
            "sent_at": row["sent_at"],
            "note": row["note"],
        }

    def queue(self, signal: PortalSignal | Mapping[str, Any]) -> tuple[PortalSignal, dict[str, Any], bool]:
        parsed = parse_portal_signal(signal) if isinstance(signal, Mapping) else signal
        if not isinstance(parsed, PortalSignal):
            raise ValueError("A validated portal signal is required.")
        payload = _json(parsed)
        fingerprint = _fingerprint(payload)
        with closing(self._connect()) as connection, connection:
            existing = connection.execute(
                "SELECT * FROM tradetron_signal_outbox WHERE event_id = ?", (parsed.event_id,)
            ).fetchone()
            if existing:
                if existing["fingerprint"] != fingerprint:
                    raise ValueError("This event ID already belongs to a different portal decision.")
                return parsed, self._visible(existing), False
            try:
                connection.execute("""
                    INSERT INTO tradetron_signal_outbox
                    (event_id, event_type, direction, sequence, expiry, payload, fingerprint, status, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, 'queued', ?)
                """, (parsed.event_id, parsed.event_type, parsed.direction, parsed.sequence,
                      parsed.expiry, payload, fingerprint, _now()))
            except sqlite3.IntegrityError as exc:
                raise ValueError("That direction and event counter is already recorded for a different portal decision.") from exc
            created = connection.execute(
                "SELECT * FROM tradetron_signal_outbox WHERE event_id = ?", (parsed.event_id,)
            ).fetchone()
        return parsed, self._visible(created), True

    def event(self, event_id: str) -> tuple[PortalSignal, dict[str, Any]] | None:
        with closing(self._connect()) as connection, connection:
            row = connection.execute("SELECT * FROM tradetron_signal_outbox WHERE event_id = ?", (event_id,)).fetchone()
        if not row:
            return None
        return parse_portal_signal(json.loads(row["payload"])), self._visible(row)

    def next_queued(self) -> tuple[PortalSignal, dict[str, Any]] | None:
        with closing(self._connect()) as connection, connection:
            row = connection.execute("""
                SELECT * FROM tradetron_signal_outbox WHERE status = 'queued'
                ORDER BY created_at, event_id LIMIT 1
            """).fetchone()
        if not row:
            return None
        return parse_portal_signal(json.loads(row["payload"])), self._visible(row)

    def update(self, event_id: str, status: str, note: str | None = None, *, sent: bool = False) -> dict[str, Any]:
        if status not in OUTBOX_STATUSES:
            raise ValueError("Unknown signal-outbox state.")
        with closing(self._connect()) as connection, connection:
            updated = connection.execute("""
                UPDATE tradetron_signal_outbox
                SET status = ?, note = ?, sent_at = CASE WHEN ? THEN ? ELSE sent_at END
                WHERE event_id = ?
            """, (status, note, sent, _now(), event_id))
            if not updated.rowcount:
                raise ValueError("Unknown paper signal event.")
            row = connection.execute("SELECT * FROM tradetron_signal_outbox WHERE event_id = ?", (event_id,)).fetchone()
        return self._visible(row)

    def recent(self, limit: int = 20) -> list[dict[str, Any]]:
        with closing(self._connect()) as connection, connection:
            rows = connection.execute("""
                SELECT * FROM tradetron_signal_outbox ORDER BY created_at DESC, event_id DESC LIMIT ?
            """, (max(1, min(limit, 100)),)).fetchall()
        return [self._visible(row) for row in rows]

    def recover_interrupted_send(self) -> int:
        """Do not replay a request whose process died mid-flight."""
        with closing(self._connect()) as connection, connection:
            updated = connection.execute("""
                UPDATE tradetron_signal_outbox
                SET status = 'uncertain',
                    note = 'The app restarted while this request was being sent. It was not retried automatically.'
                WHERE status = 'sending'
            """)
        return updated.rowcount


class TradetronSignalRuntime:
    """A paper-only sender that is intentionally inactive until a future UI arms it."""

    def __init__(self, link: Any, path: str | Path,
                 delivery_factory: Callable[[Mapping[str, str]], TradetronPaperDelivery] = TradetronPaperDelivery):
        self.link = link
        self.store = TradetronSignalStore(path)
        self.delivery_factory = delivery_factory
        self.armed = False
        recovered = self.store.recover_interrupted_send()
        self.blocked = bool(recovered)
        self.blocked_reason: str | None = (
            "An earlier signal was interrupted while sending. It was not retried; review Tradetron before continuing."
            if recovered else None
        )

    def _link_ready(self) -> bool:
        details = getattr(self.link, "details", None)
        return isinstance(details, Mapping) and bool(details)

    def arm(self, confirmation: str) -> dict[str, Any]:
        if confirmation != ARMING_CONFIRMATION:
            raise ValueError("Paper signal sending requires its exact confirmation.")
        if not self._link_ready():
            raise ValueError("Link the protected paper-only Tradetron executor first.")
        self.armed, self.blocked, self.blocked_reason = True, False, None
        return self.status()

    def disarm(self) -> dict[str, Any]:
        self.armed = False
        return self.status()

    def queue(self, signal: PortalSignal | Mapping[str, Any]) -> dict[str, Any]:
        _parsed, record, _created = self.store.queue(signal)
        return record

    def dispatch_next(self) -> dict[str, Any] | None:
        """Send one queued event, never retrying an outcome that is uncertain."""
        if not self.armed:
            raise ValueError("Paper signal runner is disarmed. No runtime value was sent.")
        if self.blocked:
            raise ValueError("Paper signal runner is blocked after an unconfirmed delivery. Review Tradetron before any retry.")
        if not self._link_ready():
            raise ValueError("The protected Tradetron link is unavailable. No runtime value was sent.")
        item = self.store.next_queued()
        if not item:
            return None
        signal, record = item
        self.store.update(record["event_id"], "sending")
        try:
            delivery = self.delivery_factory(self.link.details).signal(signal)
        except TradetronDeliveryError:
            # The endpoint may have processed the request even if the client
            # could not verify it. Never turn this into an automated retry.
            self.blocked = True
            self.blocked_reason = "Tradetron did not give a usable acknowledgement. The event was not retried. Review the deployment before continuing."
            return self.store.update(record["event_id"], "uncertain", self.blocked_reason, sent=True)
        except Exception:
            self.blocked = True
            self.blocked_reason = "Paper signal delivery failed before a usable acknowledgement. The event was not retried."
            return self.store.update(record["event_id"], "uncertain", self.blocked_reason, sent=True)
        return self.store.update(
            record["event_id"], "accepted_unconfirmed",
            "Tradetron accepted the runtime update. This is not a broker-execution confirmation.", sent=True,
        )

    def confirm_after_operator_review(self, event_id: str) -> dict[str, Any]:
        existing = self.store.event(event_id)
        if not existing:
            raise ValueError("Unknown paper signal event.")
        _signal, record = existing
        if record["status"] != "accepted_unconfirmed":
            raise ValueError("Only an accepted-but-unconfirmed paper event can be reviewed.")
        return self.store.update(
            event_id, "operator_confirmed",
            "An operator confirmed the matching paper execution in Tradetron. This app did not verify a broker fill.",
        )

    def status(self) -> dict[str, Any]:
        if self.blocked:
            message = self.blocked_reason
        elif self.armed:
            message = "Paper signal runner is armed, but no automatic strategy event producer is connected yet."
        else:
            message = "Paper signal runner is disarmed. No runtime values can be sent."
        return {
            "mode": "paper_only",
            "armed": self.armed,
            "blocked": self.blocked,
            "message": message,
            "delivery_meaning": "API accepted means Tradetron received a runtime update; it does not prove a broker or paper execution.",
            "automatic_runner": "unavailable",
            "pending_events": len([event for event in self.store.recent(100) if event["status"] == "queued"]),
            "recent_events": self.store.recent(10),
        }
