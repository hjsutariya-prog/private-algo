"""One-time delivery helpers for a verified Tradetron paper deployment.

This module intentionally sends only named runtime variables.  It cannot form a
broker order, choose an instrument, or be used for a live deployment.  The
caller must first turn a validated historical snapshot into ``PortalSignal``
objects using :mod:`tradetron_protocol`.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime
import json
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from tradetron_link import SAFE_SIGNAL_PATTERN, valid_details
from tradetron_protocol import PortalSignal, parse_portal_signal


API_URL = "https://api.tradetron.tech/api"
TIMEOUT_SECONDS = 10


class TradetronDeliveryError(RuntimeError):
    """A delivery failure that may need manual deployment review."""


@dataclass(frozen=True)
class RuntimeUpdate:
    name: str
    value: str


def _whole_snapshot_value(value: Any, label: str) -> int:
    """Convert JSON's exact numeric representation without rounding a trade."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or int(value) != value:
        raise ValueError(f"The saved {label} is not a whole number.")
    return int(value)


def entry_updates(signal: PortalSignal | Mapping[str, Any]) -> tuple[RuntimeUpdate, ...]:
    """Build a direction's entry updates with the trigger deliberately last."""
    parsed = parse_portal_signal(signal) if isinstance(signal, Mapping) else signal
    if not isinstance(parsed, PortalSignal) or parsed.event_type != "entry":
        raise ValueError("Only a validated entry signal can create an entry update.")
    prefix = "pa_" + parsed.direction.lower()
    return (
        RuntimeUpdate(prefix + "_expiry_offset", str(parsed.expiry_offset)),
        RuntimeUpdate(prefix + "_short_strike", str(parsed.short_strike)),
        RuntimeUpdate(prefix + "_hedge_strike", str(parsed.hedge_strike)),
        RuntimeUpdate(prefix + "_entry_seq", str(parsed.sequence)),
    )


def bootstrap_entry_updates(signal: PortalSignal | Mapping[str, Any]) -> tuple[RuntimeUpdate, ...]:
    """Re-arm one stale paper-test acknowledgement before its final trigger.

    This is deliberately limited to the recorded four-leg bootstrap. Normal
    portal entries must remain monotonic and must never reset a live position's
    acknowledgement value.
    """
    parsed = parse_portal_signal(signal) if isinstance(signal, Mapping) else signal
    if not isinstance(parsed, PortalSignal) or parsed.event_type != "entry":
        raise ValueError("Only a validated entry signal can create a bootstrap update.")
    return (RuntimeUpdate(parsed.direction.lower() + "_entry_seen", "0"), *entry_updates(parsed))


class TradetronPaperDelivery:
    """Deliver a validated directional signal as one Tradetron JSON request."""

    def __init__(self, details: Mapping[str, str], opener: Callable[..., Any] = urlopen):
        if not valid_details(dict(details)):
            raise ValueError("A protected Tradetron API link is required.")
        self.details = dict(details)
        self.opener = opener

    def _request(self, updates: tuple[RuntimeUpdate, ...]) -> Request:
        if not updates:
            raise ValueError("A Tradetron delivery needs at least one runtime value.")
        if len(updates) > 32:
            raise ValueError("A Tradetron delivery has too many runtime values.")
        payload: dict[str, str] = {"auth-token": self.details["auth_token"]}
        for index, update in enumerate(updates):
            if not SAFE_SIGNAL_PATTERN.fullmatch(update.name):
                raise ValueError("Runtime variable names must be simple letters, digits and underscores.")
            if not isinstance(update.value, str) or not update.value or len(update.value) > 256:
                raise ValueError("Runtime values must be short non-empty strings.")
            suffix = "" if index == 0 else str(index)
            payload["key" + suffix] = update.name
            payload["value" + suffix] = update.value
        # Tradetron's multi-value API requires one JSON POST.  The ordered
        # entry sequence is deliberately last so its strike/expiry details
        # arrive together before the set condition can become true.
        return Request(API_URL, data=json.dumps(payload, separators=(",", ":")).encode("utf-8"), headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "PrivateAlgoDesk/0.1",
        })

    def deliver(self, updates: tuple[RuntimeUpdate, ...]) -> dict[str, Any]:
        request = self._request(updates)
        try:
            response = self.opener(request, timeout=TIMEOUT_SECONDS)
            with response:
                status = response.getcode()
        except HTTPError as exc:
            raise TradetronDeliveryError(
                f"Tradetron rejected the directional paper signal (HTTP {exc.code})."
            ) from exc
        except Exception as exc:  # Network failures have an unknown remote outcome.
            raise TradetronDeliveryError(
                "Tradetron did not confirm the directional paper signal. Check the deployment before retrying."
            ) from exc
        if not 200 <= status < 300:
            raise TradetronDeliveryError(
                f"Tradetron rejected the directional paper signal (HTTP {status})."
            )
        return {"runtime_variables": [update.name for update in updates], "http_status": status}

    def entry(self, signal: PortalSignal | Mapping[str, Any]) -> dict[str, Any]:
        """Deliver one CE or PE entry as a single ordered JSON request."""
        return self.deliver(entry_updates(signal))

    def bootstrap_entry(self, signal: PortalSignal | Mapping[str, Any]) -> dict[str, Any]:
        """Deliver the explicitly approved one-time four-leg paper bootstrap."""
        return self.deliver(bootstrap_entry_updates(signal))


def snapshot_entries(snapshot: Mapping[str, Any], *, sequence: int = 1,
                     current_date: date | None = None) -> tuple[PortalSignal, PortalSignal]:
    """Create exactly CE and PE entry signals from the saved four-leg paper snapshot.

    The snapshot remains historical research data.  This merely reconstructs
    its four open option legs in an explicitly selected Live Offline deployment
    for API plumbing verification.
    """
    if not isinstance(snapshot, Mapping) or snapshot.get("kind") != "historical_paper_replay":
        raise ValueError("Only the saved historical paper replay can be used for this one-time check.")
    legs = snapshot.get("legs")
    if not isinstance(legs, list):
        raise ValueError("The saved paper replay has no open-leg list.")
    open_legs = [leg for leg in legs if isinstance(leg, Mapping) and leg.get("status") == "open"]
    if len(open_legs) != 4:
        raise ValueError("The paper check requires exactly four open legs: two shorts and two hedges.")
    by_direction: dict[str, dict[str, Mapping[str, Any]]] = {"CE": {}, "PE": {}}
    for leg in open_legs:
        direction = leg.get("option_type")
        if direction not in by_direction:
            raise ValueError("The paper replay contains a non-option leg.")
        side = leg.get("direction")
        if side == "SELL":
            kind = "short"
        elif side == "BUY" and leg.get("ledger") == direction.lower() + "_hedge":
            kind = "hedge"
        else:
            raise ValueError("The paper replay must contain only one short and one named hedge per direction.")
        if kind in by_direction[direction]:
            raise ValueError("The paper replay has duplicate directional legs.")
        by_direction[direction][kind] = leg

    signals: list[PortalSignal] = []
    as_of = str(snapshot.get("as_of", ""))[:10].replace("-", "")
    current_date = current_date or datetime.now(ZoneInfo("Asia/Kolkata")).date()
    for direction in ("CE", "PE"):
        group = by_direction[direction]
        if set(group) != {"short", "hedge"}:
            raise ValueError("Each direction needs both an open short and its open hedge.")
        short, hedge = group["short"], group["hedge"]
        contract, hedge_contract = short.get("contract"), hedge.get("contract")
        if not isinstance(contract, Mapping) or not isinstance(hedge_contract, Mapping):
            raise ValueError("The paper replay has no exact contract names for an open leg.")
        if short.get("expiry") != hedge.get("expiry") or short.get("quantity") != hedge.get("quantity"):
            raise ValueError("A paper short and hedge must have the same expiry and quantity.")
        expiry = str(short.get("expiry"))
        # A historical replay ends at the last completed session, which can be
        # the previous calendar day.  Compare against today's IST month, not
        # the snapshot valuation date, and never silently trade stale contracts.
        if not expiry.startswith(current_date.strftime("%Y-%m")):
            raise ValueError("The saved contracts are no longer the current monthly expiry; do not bootstrap them.")
        payload = {
            "event_id": f"paper-bootstrap-{direction.lower()}-{as_of}-{sequence:03d}",
            "event_type": "entry", "direction": direction, "sequence": sequence,
            "expiry": expiry, "expiry_offset": 0, "quantity": _whole_snapshot_value(short.get("quantity"), "quantity"),
            "short_symbol": contract.get("symbol"), "short_strike": _whole_snapshot_value(short.get("strike"), "short strike"),
            "hedge_symbol": hedge_contract.get("symbol"), "hedge_strike": _whole_snapshot_value(hedge.get("strike"), "hedge strike"),
            "signal_hour_close": short.get("risk_reference_spot"),
            "stop_spot": short.get("stop_spot"), "target_spot": short.get("target_spot"),
        }
        signals.append(parse_portal_signal(payload))
    return tuple(signals)  # CE first, then PE.


def dispatch_snapshot_entries(details: Mapping[str, str], snapshot: Mapping[str, Any], *, opener=urlopen) -> dict[str, Any]:
    """Dispatch the exact four-leg snapshot as two hedge-first entry sets."""
    delivery = TradetronPaperDelivery(details, opener=opener)
    # Earlier manual plumbing checks acknowledged sequence 1 without retaining
    # a position. Use the next sequence and explicitly re-arm only this paper
    # bootstrap so the visible 1 > 1 state cannot silently skip the test.
    ce, pe = snapshot_entries(snapshot, sequence=2)
    return {"mode": "paper_bootstrap_only", "strategy_id": details["strategy_id"],
            "entries": [{"direction": ce.direction, "delivery": delivery.bootstrap_entry(ce)},
                        {"direction": pe.direction, "delivery": delivery.bootstrap_entry(pe)}]}
