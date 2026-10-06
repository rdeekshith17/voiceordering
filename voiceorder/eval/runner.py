"""Runner: every golden script x every POS profile x N runs.

Usage:
    ANTHROPIC_API_KEY=... ANTHROPIC_MODEL=<model-id> \
      python -m voiceorder.eval.runner --runs 5 --out eval/out

Phase 2 gate: every script ends with exactly the expected cart, 5 runs each,
on all three profiles.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from ..agent.llm import AnthropicClient, MissingCredentials
from ..core.catalog import Catalog
from .harness import load_scripts, run_script

DEFAULT_SCRIPTS = Path(__file__).resolve().parent / "scripts"
DEFAULT_CATALOG = Path(__file__).resolve().parent.parent / "fixtures" / "menu_taqueria.json"


def main() -> int:
    parser = argparse.ArgumentParser(description="VoiceOrderAI golden-script eval runner")
    parser.add_argument("--scripts", default=str(DEFAULT_SCRIPTS))
    parser.add_argument("--profiles", default="square_like,clover_like,toast_like")
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--model", default=os.environ.get("ANTHROPIC_MODEL", ""))
    parser.add_argument("--caller", choices=["scripted", "llm"], default="scripted")
    parser.add_argument("--only", default="", help="comma-separated script ids to run")
    parser.add_argument("--out", default="eval/out")
    args = parser.parse_args()

    if not args.model:
        print("Set ANTHROPIC_MODEL to an Anthropic model id (or pass --model).", file=sys.stderr)
        return 2
    try:
        llm = AnthropicClient()
    except MissingCredentials as exc:
        print(f"Cannot run evals: {exc}", file=sys.stderr)
        return 2

    catalog = Catalog.from_json(args.catalog if hasattr(args, "catalog") else DEFAULT_CATALOG)
    scripts = load_scripts(args.scripts)
    if args.only:
        wanted = set(args.only.split(","))
        scripts = [s for s in scripts if s.id in wanted]
    profiles = [p.strip() for p in args.profiles.split(",") if p.strip()]

    results: list[dict] = []
    total = len(scripts) * len(profiles) * args.runs
    done = 0
    for script in scripts:
        for profile in profiles:
            if profile not in script.pos_profiles:
                continue
            for run in range(args.runs):
                done += 1
                print(f"[{done}/{total}] {script.id} x {profile} run {run + 1}/{args.runs} ...",
                      end=" ", flush=True)
                record = run_script(
                    script, profile=profile, llm=llm, model=args.model,
                    catalog=catalog, outdir=args.out, run_index=run,
                    caller_kind=args.caller,
                )
                results.append(record)
                print("PASS" if record["passed"] else "FAIL")

    passed = sum(1 for r in results if r["passed"])
    report = {
        "model": args.model,
        "caller": args.caller,
        "runs_per_script": args.runs,
        "total": len(results),
        "passed": passed,
        "failed": len(results) - passed,
        "results": results,
    }
    out_path = Path(args.out) / "report.json"
    out_path.write_text(json.dumps(report, indent=2))
    print(f"\n{passed}/{len(results)} passed. Report: {out_path}")
    for r in results:
        if not r["passed"]:
            print(f"  FAIL {r['script']} x {r['profile']} run {r['run']}:")
            for failure in r["failures"]:
                print(f"    - {failure}")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
