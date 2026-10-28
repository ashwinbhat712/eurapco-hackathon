"""Measure supervisor routing against evals/routing/golden.json (calls the real model).

    python -m evals.routing.run                 # dev split
    python -m evals.routing.run --split holdout
    python -m evals.routing.run --split all --only A05 A07
"""

import argparse
import asyncio
import json
import sys
from collections import defaultdict

from dotenv import load_dotenv

from app.contracts import ROOT
from app.llm import make_client
from app.supervisor import SupervisorUnavailable, classify

GOLDEN = ROOT / "evals" / "routing" / "golden.json"


def load_cases(split, only):
    cases = json.loads(GOLDEN.read_text(encoding="utf-8"))["cases"]
    if split != "all":
        cases = [case for case in cases if case["split"] == split]
    if only:
        cases = [case for case in cases if case["id"] in only]
    return cases


def request_for(case):
    if "source_file" in case:
        return json.loads((ROOT / case["source_file"]).read_text(encoding="utf-8"))
    return case["event"]


async def evaluate(case, client, limit):
    async with limit:
        try:
            decision = await classify(request_for(case), client)
        except SupervisorUnavailable as exc:
            return {"id": case["id"], "split": case["split"], "ok": False, "error": str(exc)}
    got = {"agent": decision.agent, "category": decision.request_category}
    return {"id": case["id"], "split": case["split"], "ok": got in case["accepted"], "got": got,
            "accepted": case["accepted"], "rationale": decision.rationale, "attempts": decision.attempts}


async def run(cases, concurrency):
    client = make_client()
    limit = asyncio.Semaphore(concurrency)
    try:
        return await asyncio.gather(*(evaluate(case, client, limit) for case in cases))
    finally:
        await client.a_client.close()


def report(results):
    totals = defaultdict(lambda: [0, 0])
    for result in results:
        totals[result["split"]][0] += result["ok"]
        totals[result["split"]][1] += 1
        if result["ok"]:
            print(f"PASS {result['id']}: {result['got']['agent']}/{result['got']['category']}")
        elif "error" in result:
            print(f"FAIL {result['id']}: {result['error']}")
        else:
            expected = " | ".join(f"{a['agent']}/{a['category']}" for a in result["accepted"])
            print(f"FAIL {result['id']}: got {result['got']['agent']}/{result['got']['category']}, "
                  f"expected {expected}\n     rationale: {result['rationale']}")
    print()
    for split, (passed, total) in sorted(totals.items()):
        print(f"{split}: {passed}/{total} ({passed / total:.0%})")


def main():
    load_dotenv(ROOT / ".env")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--split", choices=("dev", "holdout", "all"), default="dev")
    parser.add_argument("--only", nargs="+", metavar="ID", help="Run only these case ids.")
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--out", help="Also write the per-case results to this JSON file.")
    args = parser.parse_args()

    cases = load_cases(args.split, set(args.only or ()))
    if not cases:
        print("No cases selected.", file=sys.stderr)
        return 1
    results = asyncio.run(run(cases, args.concurrency))
    report(results)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            json.dump(results, handle, indent=2, ensure_ascii=False)
    return 0 if all(result["ok"] for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
