"""Repeat the engine acceptance checks without logging in or placing orders."""
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PROJECT = ROOT.parent
OLD = Path(r"C:\Users\Harshal Sutariya\buildweek\algo")
NODE = Path(r"C:\Users\Harshal Sutariya\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin\node.exe")
old_runner = "import sys; sys.path.append(" + repr(str(OLD / ".venv/Lib/site-packages")) + "); import pytest; raise SystemExit(pytest.main(['-q','-p','no:cacheprovider','tests/test_thousand_levels_hourly.py','tests/test_short_option_hedge_model.py','tests/test_short_option_rollover.py']))"
checks = [
    ("new_app_python", PROJECT, [sys.executable, "-B", "-m", "unittest", "discover", "-s", "tests", "-q"]),
    ("new_app_report_ui", PROJECT, [str(NODE), "--test", "tests/report_ui.test.cjs"]),
    ("reference_engine_focused", OLD, [sys.executable, "-B", "-c", old_runner]),
    ("audit_reproductions", PROJECT, [sys.executable, "-B", "validation/audit_reproductions.py"]),
    ("identical_cached_candles", PROJECT, [sys.executable, "-B", "validation/compare_engines.py"]),
]
results = []
destination = ROOT / "verification-run.json"
previous_runs = []
if destination.exists():
    previous = json.loads(destination.read_text(encoding="utf-8"))
    previous_runs = previous.pop("previous_runs", []) + [previous]
for name, cwd, command in checks:
    print("Checking", name, flush=True)
    start = time.monotonic()
    done = subprocess.run(command, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=240)
    results.append({"name": name, "exit_code": done.returncode, "elapsed_seconds": round(time.monotonic()-start, 2),
                    "command": command, "cwd": str(cwd), "stdout": done.stdout, "stderr": done.stderr})
    print(name, "PASS" if done.returncode == 0 else "FAIL", results[-1]["elapsed_seconds"], flush=True)
    destination.write_text(json.dumps({"checked_at_utc": datetime.now(timezone.utc).isoformat(), "checks": results, "previous_runs": previous_runs}, indent=2), encoding="utf-8")
    if done.returncode:
        print(done.stderr[-2000:], flush=True)
        break
raise SystemExit(0 if len(results) == len(checks) and all(r["exit_code"] == 0 for r in results) else 1)
