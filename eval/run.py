"""CLI entry point for eval experiments.

Usage:
    python -m eval.run --experiment routing --policy smart-routing \
        --dataset eval/golden_dataset.json
    python -m eval.run --experiment frontier \
        --policies smart-routing all-haiku all-sonnet all-opus
    python -m eval.run --experiment routing --policy smart-routing --store
"""

import argparse
import json
import logging
import sys

from eval import experiments

logger = logging.getLogger(__name__)

_DEFAULT_DATASET = "eval/golden_dataset.json"


def _print_experiment(exp: experiments.ExperimentResult) -> None:
    payload = {
        "run_id": exp.run_id,
        "policy": exp.policy,
        "dataset_version": exp.dataset_version,
        "total_requests": exp.total_requests,
        "avg_quality": exp.avg_quality,
        "avg_cost_usd": exp.avg_cost_usd,
        "total_cost_usd": exp.total_cost_usd,
        "mismatches": [
            {
                "id": r.entry_id,
                "expected": r.expected_tier,
                "assigned": r.assigned_tier,
                "prompt": r.prompt[:80],
            }
            for r in exp.results
            if r.quality_score < 1.0
        ],
    }
    sys.stdout.write(json.dumps(payload, indent=2) + "\n")


def _print_frontier(frontier: experiments.FrontierResult) -> None:
    payload = {
        "dataset_version": frontier.dataset_version,
        "points": [
            {
                "policy": p.policy,
                "avg_quality": p.avg_quality,
                "avg_cost_usd": p.avg_cost_usd,
                "pareto_optimal": p.pareto_optimal,
            }
            for p in frontier.points
        ],
    }
    sys.stdout.write(json.dumps(payload, indent=2) + "\n")


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)

    parser = argparse.ArgumentParser(prog="python -m eval.run")
    parser.add_argument(
        "--experiment",
        choices=["routing", "frontier"],
        required=True,
    )
    parser.add_argument("--policy", default="smart-routing")
    parser.add_argument(
        "--policies",
        nargs="+",
        default=["smart-routing", "all-haiku", "all-sonnet", "all-opus"],
        help="Policies for the frontier experiment",
    )
    parser.add_argument("--dataset", default=_DEFAULT_DATASET)
    parser.add_argument(
        "--store",
        action="store_true",
        help="Persist the run to the eval_runs table (routing experiment only)",
    )
    args = parser.parse_args(argv)

    dataset = experiments.load_dataset(args.dataset)

    if args.experiment == "routing":
        exp = experiments.run_routing_experiment(dataset, args.policy)
        _print_experiment(exp)
        if args.store:
            experiments.store_run(exp, passed=True)
    else:
        frontier = experiments.run_cost_quality_frontier(dataset, args.policies)
        _print_frontier(frontier)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
