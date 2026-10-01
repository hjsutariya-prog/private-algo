"""Named BANKNIFTY strategy rules; never silently combine timing conventions."""
from copy import deepcopy

COMPARISON = "hour_boundary_uncertain_v1"
REVIEW = "review_20s_stop_first_v1"
CLOSE_BOUNDARY = "hour_close_boundary_uncertain_v2"
CLOSE_REVIEW = "hour_close_review_20s_stop_first_v2"
CURRENT_MODES = (CLOSE_BOUNDARY, CLOSE_REVIEW)
STRATEGIES = {
    COMPARISON: {
        "id": COMPARISON, "label": "Hourly boundary · uncertain collisions (reference comparison)",
        "decision_delay_seconds": 0, "entry_delay_minutes": 0,
        "collision_policy": "uncertain", "intraminute_valuation": "minute_end",
    },
    REVIEW: {
        "id": REVIEW, "label": "20-second review · next-minute entry · assumed stop first",
        "decision_delay_seconds": 20, "entry_delay_minutes": 1,
        "collision_policy": "stop_first", "intraminute_valuation": "minute_start",
    },
}
for identity, original, label in (
    (CLOSE_BOUNDARY, COMPARISON, "Hourly boundary · fixed signal-hour close SL/target"),
    (CLOSE_REVIEW, REVIEW, "20-second review · next-minute entry · fixed signal-hour close SL/target"),
):
    STRATEGIES[identity] = {**STRATEGIES[original], "id": identity, "label": label,
                            "risk_reference_basis": "completed_signal_hour_close",
                            "entry_outside_risk_policy": "skip"}


def strategy_config(mode=COMPARISON):
    if not isinstance(mode, str) or mode not in STRATEGIES:
        raise ValueError("Choose a named BANKNIFTY strategy timing rule.")
    return {**deepcopy(STRATEGIES[mode]), "instrument": "BANKNIFTY", "stop_points": 500,
            "risk_reference_basis": STRATEGIES[mode].get("risk_reference_basis", "execution_entry_spot"),
            "target_points": 1000, "signal_grid_points": 1000, "weekly_entry_cap": None,
            "positions_per_direction": 1, "opposite_directions_independent": True}
