"""Score the dispute agent against labelled scenarios.

    python scripts/eval_agent.py              # the configured AI (Gemini / any OpenAI-compatible / Claude)
    python scripts/eval_agent.py --rules      # rule-based fallback, offline
    python scripts/eval_agent.py --out docs/eval-results.md

Each scenario lists the verdicts a careful human analyst would accept. The agent passes a case when its
recommendation is in that set. The report also shows the win probability and how long each case took.
Runs against a throwaway copy of the demo data, so it never touches data/state.json.
"""

import argparse
import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="dd-eval-")
os.environ["FORCE_MOCK"] = "1"
sys.path.insert(0, str(ROOT))

from app import agent  # noqa: E402
from app.config import settings  # noqa: E402
from app.heuristic import heuristic_investigate  # noqa: E402
from app.mock_paypal import MockPayPal  # noqa: E402
from app.store import store  # noqa: E402
from app.tools import ToolContext  # noqa: E402

CASES = [
    ("PP-D-27461", {"FIGHT"}, "Not received, but delivered and signed for at the PayPal address"),
    ("PP-D-27488", {"ACCEPT", "OFFER"}, "Photos and carrier log show transit damage; packed without dividers"),
    ("PP-D-27502", {"FIGHT"}, "Unauthorized claim from a repeat customer who asked for gift wrap"),
    ("PP-D-27515", {"FIGHT"}, "Refund-not-processed, but the PayPal refund already completed"),
    ("PP-D-27530", {"OFFER"}, "One mug darker than the photo; listing discloses handmade variation"),
    ("PP-D-27544", {"FIGHT"}, "'Duplicate' charge was two orders to two addresses"),
    ("SIM-1", {"OFFER", "ACCEPT"}, "Package stalled in transit; buyer's email unanswered"),
    ("SIM-2", {"ACCEPT"}, "Account-takeover pattern: shipment re-routed to a freight forwarder"),
]


async def main(use_rules: bool, out: str | None) -> int:
    store.load()
    provider = MockPayPal()
    sims = [await provider.simulate_new_dispute(), await provider.simulate_new_dispute()]
    ids = {"SIM-1": sims[0]["dispute_id"], "SIM-2": sims[1]["dispute_id"]}
    ctx = ToolContext(provider, await provider.list_disputes())
    engine = "rules" if use_rules or not settings.ai_enabled else "claude"
    sem = asyncio.Semaphore(max(1, settings.ai_concurrency) if engine == "claude" else 3)

    async def run(case):
        label, accepted, why = case
        did = ids.get(label, label)
        async with sem:
            t0 = time.monotonic()
            try:
                if engine == "claude":
                    result = await agent.investigate(did, ctx, lambda step: None)
                else:
                    result = await heuristic_investigate(did, ctx, lambda step: None)
                rec, win, err = result["recommendation"], result["win_probability"], ""
            except Exception as exc:  # report, don't abort the whole eval
                rec, win, err = "ERROR", None, str(exc)[:120]
            return label, did, why, accepted, rec, win, time.monotonic() - t0, err

    rows = await asyncio.gather(*(run(c) for c in CASES))
    passed = sum(1 for r in rows if r[4] in r[3])
    model = settings.model_label if engine == "claude" else "rule-based fallback"
    lines = [
        f"# Agent evaluation - {model}",
        "",
        f"**{passed} / {len(rows)} scenarios passed.**",
        "",
        "| Case | Scenario | Accepted verdicts | Agent verdict | Win % | Time | Result |",
        "|---|---|---|---|---|---|---|",
    ]
    for label, did, why, accepted, rec, win, secs, err in rows:
        ok = "✅" if rec in accepted else "❌"
        lines.append(f"| {label} | {why} | {', '.join(sorted(accepted))} | {rec}{' - ' + err if err else ''} | "
                     f"{'' if win is None else win} | {secs:.1f}s | {ok} |")
    report = "\n".join(lines) + "\n"
    print(report)
    if out:
        Path(out).write_text(report, encoding="utf-8")
    await agent.close_client()
    return 0 if passed == len(rows) else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--rules", action="store_true", help="evaluate the rule-based fallback instead of Claude")
    parser.add_argument("--out", help="also write the markdown report to this file")
    args = parser.parse_args()
    sys.exit(asyncio.run(main(args.rules, args.out)))
