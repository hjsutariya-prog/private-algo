"""Read and retain exact historical BANKNIFTY option candles for a replay.

This module never creates orders.  A listed option contract is not evidence of
an option price: only a valid candle for the same contract and minute is saved.
"""
from datetime import date, datetime, timedelta
from decimal import Decimal

from option_prices import OptionPriceBook, contract_key
from range_spot import history_windows
from market_sessions import download_bounds


STRIKE_GRID = Decimal("500")


def _when(value):
    return datetime.fromisoformat(value) if isinstance(value, str) else value


def _key(contract):
    return contract_key(contract)


def banknifty_catalog(rows):
    """Return current, usable BANKNIFTY options keyed by exact identity.

    Kite's daily catalogue is the only source of a historical endpoint token.
    Expired contracts absent from that catalogue can still use archived candles,
    but cannot be fetched again by guessing or reusing a token.
    """
    catalog = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        kind = row.get("instrument_type")
        name, symbol = row.get("name"), row.get("tradingsymbol")
        if (kind not in ("CE", "PE") or row.get("exchange") != "NFO"
                or not isinstance(symbol, str) or not symbol
                or not (name == "BANKNIFTY" or symbol.startswith("BANKNIFTY"))):
            continue
        try:
            token = row["instrument_token"]
            if type(token) is not int or token <= 0:
                continue
            contract = {"instrument": "BANKNIFTY", "expiry": date.fromisoformat(row["expiry"]).isoformat(),
                        "option_type": kind, "strike": float(Decimal(str(row["strike"]))), "symbol": symbol,
                        "instrument_token": token}
            key = _key(contract)
        except (KeyError, TypeError, ValueError):
            continue
        previous = catalog.get(key)
        if previous and (previous["symbol"] != symbol or previous["instrument_token"] != token):
            raise ValueError("Kite returned conflicting BANKNIFTY contract identities. No option prices were selected.")
        catalog[key] = contract
    return catalog


def enrich(contract, catalog):
    """Add a current exact symbol/token when the contract is still listed."""
    if not contract:
        return None
    try:
        matched = catalog.get(_key(contract))
        return dict(matched) if matched else None
    except (KeyError, TypeError, ValueError):
        return None


def selling_candidates(catalog, position):
    """All listed ATM/ITM 500-point strikes, nearest ATM first.

    Every eligible listed strike is checked at the entry minute.  There is no
    guessed strike range which could silently miss the premium closest to ₹1,000.
    """
    option_type, expiry = position["option_type"], position["expiry"]
    spot = Decimal(str(position["entry_spot"]))
    choices = []
    for contract in catalog.values():
        strike = Decimal(str(contract["strike"]))
        if (contract["expiry"] != expiry or contract["option_type"] != option_type
                or strike % STRIKE_GRID != 0):
            continue
        if option_type == "CE" and strike <= spot or option_type == "PE" and strike >= spot:
            choices.append(contract)
    return sorted(choices, key=lambda item: abs(Decimal(str(item["strike"])) - spot))


def _candle(row):
    return [row["timestamp_ist"], row["open"], row["high"], row["low"], row["close"], row["volume"]]


def _record(archive, book, contract, rows, source="kite_historical"):
    """Store only candles that pass the shared price and session validation.

    Provider responses can include a minute that is outside the session calendar
    we have accepted for this replay.  It is not safe to use that observation,
    but one rejected row must not discard valid observations for the same
    contract or turn the whole research run into a failure.
    """
    # Instrument tokens are a short-lived lookup mechanism, not part of the
    # durable price identity or the report returned to the browser.
    stored_contract = {key: value for key, value in contract.items() if key != "instrument_token"}
    validator, good, rejected = OptionPriceBook(), [], {}
    for item in rows:
        try:
            row = _candle(item)
            validator.add_candle(stored_contract, row, "validation", source)
        except (KeyError, TypeError, ValueError) as exc:
            book.rejected += 1
            reason = str(exc) or "Option candle could not be validated."
            rejected[reason] = rejected.get(reason, 0) + 1
            continue
        good.append(row)
    if not good:
        return 0, rejected
    archive.record_candles(stored_contract, good, source)
    for row in good:
        # This was already validated above. Existing archive conflicts remain
        # visible to lookup, rather than causing a later candle to be chosen.
        book.add_candle(stored_contract, row, "fresh:" + contract["symbol"] + ":" + row[0], source)
    return len(good), rejected


def _add_rejections(summary, rejected):
    for reason, count in rejected.items():
        summary["rejected_candles"] += count
        summary["rejected_reasons"][reason] = summary["rejected_reasons"].get(reason, 0) + count


def _minute_rows(client, contract, at):
    moment = _when(at)
    return client.historical_minutes(contract["instrument_token"], moment, moment + timedelta(seconds=59))


def _span_rows(client, contract, start, finish, check_active):
    rows = []
    for first, last in history_windows(_when(start).date(), _when(finish).date()):
        check_active()
        rows.extend(client.historical_minutes(contract["instrument_token"], *download_bounds(first, last)))
    return rows


def collect_historical_option_prices(client, archive, candles, spot_ledger, config, first_date, last_date, *, assumed_holidays=(), check_active=lambda: None, progress=None):
    """Fetch exact option data where Kite still supplies exact contract tokens.

    First collect each short candidate's entry-minute price, then select the
    recorded premium closest to ₹1,000.  Next collect the complete replay spans
    for selected shorts and known hedge strikes.  Missing contract/candle data is
    explicitly left for the option model's labelled estimate fallback.
    """
    report = progress or (lambda *_: None)
    from option_model import simulate_options

    preliminary = simulate_options(candles, spot_ledger, config=config, option_prices=OptionPriceBook(),
                                   assumed_holidays=assumed_holidays, check_active=check_active)
    catalog = banknifty_catalog(client.instruments("NFO"))
    book = archive.snapshot(first_date, last_date, check_active)
    summary = {"catalogued_contracts": len(catalog), "candidate_contracts_checked": 0,
               "candidate_candles_saved": 0, "lifecycle_contracts_checked": 0,
               "lifecycle_candles_saved": 0, "unavailable_contracts": 0,
               "rejected_candles": 0, "rejected_reasons": {}, "notes": []}

    sell_positions = preliminary["ledgers"]["selling"]
    candidate_groups = []
    for position in sell_positions:
        choices = selling_candidates(catalog, position)
        candidate_groups.append((position, choices))
        if not choices:
            summary["unavailable_contracts"] += 1
            summary["notes"].append(f"No currently listed {position['option_type']} contracts match the historical expiry {position['expiry']} for short selection.")

    total_candidates = sum(len(choices) for _, choices in candidate_groups)
    checked = 0
    for position, choices in candidate_groups:
        for contract in choices:
            check_active()
            checked += 1
            report("options", checked, max(total_candidates, 1), f"Checking recorded option entry price {checked} of {total_candidates}…")
            observation, _ = book.lookup(contract, position["entry_at"], field="open", side="SELL")
            if observation is None:
                rows = _minute_rows(client, contract, position["entry_at"])
                saved, rejected = _record(archive, book, contract, rows)
                summary["candidate_candles_saved"] += saved
                _add_rejections(summary, rejected)
            summary["candidate_contracts_checked"] += 1

    # This second dry calculation locks the selected short contract from the
    # recorded entry minute before any later prices are fetched.
    selected = simulate_options(candles, spot_ledger, config=config, option_prices=book,
                                assumed_holidays=assumed_holidays, check_active=check_active)

    spans = {}
    for ledger in ("selling", "ce_hedge", "pe_hedge"):
        for position in selected["ledgers"][ledger]:
            contract = enrich(position.get("contract"), catalog)
            if not contract:
                summary["unavailable_contracts"] += 1
                continue
            key = _key(contract)
            finish = position.get("exit_at") or position.get("mark_at") or position["entry_at"]
            current = spans.get(key)
            if current is None:
                spans[key] = {"contract": contract, "start": position["entry_at"], "finish": finish}
            else:
                current["start"] = min(current["start"], position["entry_at"])
                current["finish"] = max(current["finish"], finish)

    for index, item in enumerate(spans.values(), 1):
        check_active()
        report("options", index, max(len(spans), 1), f"Downloading recorded prices for option contract {index} of {len(spans)}…")
        rows = _span_rows(client, item["contract"], item["start"], item["finish"], check_active)
        summary["lifecycle_contracts_checked"] += 1
        saved, rejected = _record(archive, book, item["contract"], rows)
        summary["lifecycle_candles_saved"] += saved
        _add_rejections(summary, rejected)

    if not catalog:
        summary["notes"].append("Kite returned no usable current BANKNIFTY option contracts. Recorded option prices could not be fetched for this replay.")
    if summary["unavailable_contracts"]:
        summary["notes"].append("Some historical contracts are no longer in Kite's current catalogue. Their missing prices remain labelled estimates; no token was guessed or reused.")
    if summary["rejected_candles"]:
        reasons = "; ".join(f"{count} × {reason}" for reason, count in summary["rejected_reasons"].items())
        summary["notes"].append(f"{summary['rejected_candles']} provider option candle(s) were excluded because they did not pass price or exchange-session checks ({reasons}). Missing prices remain labelled estimates.")
    return book, summary
