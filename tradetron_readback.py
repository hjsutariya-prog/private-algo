"""Import and retain the official Tradetron deployment CSV without guessing API access.

Tradetron's documented API accepts runtime values but does not provide a
documented positions/fills read endpoint.  The deployment page's **Download
data** CSV is therefore the supported read-back source.  An import is clearly
labelled as user-imported Tradetron data: it never becomes a broker fill and
never automatically confirms a portal signal.
"""
from __future__ import annotations

import csv
from contextlib import closing
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import sqlite3
from typing import Any


_REQUIRED = frozenset({"Instrument", "expiry", "option_type", "txn_type", "quantity", "price"})
_LIMIT = 250_000


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _number(value: Any) -> float | None:
    if value is None:
        return None
    text = str(value).strip().replace(",", "")
    if not text:
        return None
    try:
        value = float(text)
    except ValueError:
        return None
    return value if value == value and value not in (float("inf"), float("-inf")) else None


def _whole(value: Any) -> int | None:
    number = _number(value)
    if number is None or not number.is_integer() or number < 0:
        return None
    return int(number)


def parse_export(text: str) -> list[dict[str, Any]]:
    """Parse only documented-export columns; reject malformed or oversized input."""
    if not isinstance(text, str) or not text.strip():
        raise ValueError("Paste the full Tradetron Download data CSV.")
    if len(text.encode("utf-8")) > _LIMIT:
        raise ValueError("This Tradetron CSV is too large to import in one request.")
    try:
        reader = csv.DictReader(io.StringIO(text))
        headers = set(reader.fieldnames or [])
    except csv.Error as exc:
        raise ValueError("The pasted Tradetron data is not a valid CSV.") from exc
    missing = sorted(_REQUIRED - headers)
    if missing:
        raise ValueError("This is not a Tradetron Download data CSV; missing " + ", ".join(missing) + ".")
    rows: list[dict[str, Any]] = []
    try:
        for index, source in enumerate(reader, start=2):
            if source is None or not any(str(value or "").strip() for value in source.values()):
                continue
            kind = str(source.get("option_type") or "").strip().upper()
            side = str(source.get("txn_type") or "").strip().upper()
            quantity = _whole(source.get("quantity"))
            instrument = str(source.get("Instrument") or "").strip()
            if kind not in {"CE", "PE"} or side not in {"BUY", "SELL"} or not instrument or quantity is None:
                raise ValueError(f"Tradetron row {index} has an invalid option, side, instrument or quantity.")
            strike = _whole(source.get("strike"))
            rows.append({
                "order_id": str(source.get("order_id") or "").strip() or None,
                "instrument": instrument,
                "underlying": str(source.get("underlying") or "").strip() or None,
                "expiry": str(source.get("expiry") or "").strip() or None,
                "option_type": kind,
                "txn_type": side,
                "quantity": quantity,
                "quantity_pending": _whole(source.get("quantity_pending")),
                "price": _number(source.get("price")),
                "strike": strike,
                "entry_date": str(source.get("entry_date") or "").strip() or None,
                "entry_time": str(source.get("entry_time") or "").strip() or None,
                "run_counter": _whole(source.get("run_counter")),
            })
    except csv.Error as exc:
        raise ValueError("The pasted Tradetron CSV ended unexpectedly.") from exc
    return rows


def _summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    groups: dict[tuple[Any, ...], dict[str, Any]] = {}
    for row in rows:
        key = (row["instrument"], row["expiry"], row["option_type"], row["strike"])
        group = groups.setdefault(key, {
            "instrument": row["instrument"], "expiry": row["expiry"], "option_type": row["option_type"],
            "strike": row["strike"], "net_quantity": 0, "reported_price": None,
            "orders": 0, "pending_quantity": 0,
        })
        group["net_quantity"] += row["quantity"] if row["txn_type"] == "BUY" else -row["quantity"]
        group["orders"] += 1
        group["pending_quantity"] += row["quantity_pending"] or 0
        if row["price"] is not None:
            group["reported_price"] = row["price"]
    positions = sorted(groups.values(), key=lambda item: (item["instrument"], item["option_type"]))
    return {
        "source": "Tradetron Download data CSV · user imported",
        "rows": len(rows),
        "net_positions": [item for item in positions if item["net_quantity"]],
        "all_instruments": positions,
        "limitations": [
            "Tradetron's published signal API is one-way; this CSV is the supported deployment read-back source.",
            "The export does not prove a broker fill and does not include a live option mark or a complete P&L field.",
            "Importing a CSV never confirms a pending portal signal automatically.",
        ],
    }


class TradetronReadbackStore:
    """Per-deployment history of user-imported official Tradetron CSV snapshots."""
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as db, db:
            db.execute("""CREATE TABLE IF NOT EXISTS tradetron_readbacks (
                imported_at TEXT PRIMARY KEY, payload TEXT NOT NULL
            )""")

    def _connect(self):
        db = sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        return db

    def import_csv(self, text: str) -> dict[str, Any]:
        rows = parse_export(text)
        result = _summary(rows)
        imported_at = _now()
        result["imported_at"] = imported_at
        with closing(self._connect()) as db, db:
            db.execute("INSERT INTO tradetron_readbacks (imported_at, payload) VALUES (?, ?)",
                       (imported_at, json.dumps(result, sort_keys=True, separators=(",", ":"))))
        return result

    def status(self) -> dict[str, Any]:
        with closing(self._connect()) as db:
            row = db.execute("SELECT payload FROM tradetron_readbacks ORDER BY imported_at DESC LIMIT 1").fetchone()
        if not row:
            return {"status": "not_imported", "source": "Tradetron Download data CSV", "message":
                    "No Tradetron deployment data has been imported yet. Download it from that deployment's three-dot menu, then paste the CSV here."}
        result = json.loads(row["payload"])
        result["status"] = "imported"
        result["message"] = "Latest local copy of the official Tradetron deployment export. It is not a direct API read or broker confirmation."
        return result
