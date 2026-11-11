#!/usr/bin/env python3
"""Run the three synthetic demo stories in an isolated database."""
import argparse
import asyncio
import json
import sys
from pathlib import Path
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import ROOT, Settings
from app.gemini import GeminiClient
from app.model import FakeClient
from app.orchestrator import Orchestrator
from app.store import Store, utcnow


async def run(mock=False):
    load_dotenv(ROOT / ".env", override=False)
    run_id = utcnow().strftime("%Y%m%d-%H%M%S-%f")
    output = ROOT / "out" / "demo" / run_id
    settings = Settings(db_path=str(output / "lendix.db"), output_dir=str(output / "documents"), demo_skip_cooling=True)
    if not mock:
        settings.validate()
    store = Store(settings.db_path)
    store.seed(ROOT / "data" / "seed.json")
    model = FakeClient() if mock else GeminiClient(settings, store)
    office = Orchestrator(store, settings, model)
    steps = [
        ("ops", "review L-1042"),
        ("ops", "add bank statement L-1042"),
        ("credit_officer", "decline L-1042"),
        ("ops", "review L-1043"),
        ("credit_officer", "approve L-1043"),
        ("borrower", "accept KFS L-1043"),
        ("ops", "disburse L-1043"),
        ("borrower", "pay EMI L-1043"),
        ("ops", "close L-1043"),
        ("borrower", "I lost my job and can't pay L-1044"),
    ]
    print("Lendix Finance · synthetic demo · " + ("mock model" if mock else "live model"))
    print("Cooling wait skipped for this isolated demonstration; no money moves.")
    try:
        for role, message in steps:
            print(f"\n[{role}] {message}")
            print(await office.run(message, role, "script-demo"))
        expected = {"L-1042": "declined", "L-1043": "closed", "L-1044": "restructure_requested"}
        failures = [loan for loan, status in expected.items() if store.get("loan", loan)["status"] != status]
        events = store.all_events()
        (output / "events.json").write_text(json.dumps(events, indent=2, ensure_ascii=False))
        print("\nEvent log:")
        for item in events:
            # Do not print artifact capability URLs; the files are in the output directory.
            print(f"{item['id']:04d} | {item['kind']:12} | {item['agent']:13} | {item['text']}")
        print(f"\nDocuments and event log: {output}")
        if failures:
            raise RuntimeError("Demo incomplete for " + ", ".join(failures) + "; inspect the event log and retry with a fresh run.")
        print("All three stories completed.")
    finally:
        if not mock:
            await model.close()
        store.engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mock", action="store_true", help="Use fixed structured outputs for an offline rehearsal; the default uses the real model.")
    options = parser.parse_args()
    try:
        asyncio.run(run(options.mock))
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from None
