"""CI quality gate: fail the build on routing quality or cost regression.

Runs the golden dataset through the smart-routing policy and fails if:
  - average routing quality < QUALITY_THRESHOLD, or
  - average cost regressed > COST_REGRESSION_PCT vs the last passing
    baseline in eval_runs (skipped when no baseline or no DB available —
    CI environments without Postgres still get the quality check).

Outputs JUnit XML so CI systems render per-check results natively.

Usage:
    python -m eval.ci_gate [--dataset eval/golden_dataset.json]
        [--policy smart-routing] [--junit-out eval-results.xml] [--store]
"""

import argparse
import logging
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

from eval import experiments

logger = logging.getLogger(__name__)

QUALITY_THRESHOLD: float = 0.85
COST_REGRESSION_PCT: float = 0.10


def _write_junit(
    path: Path,
    policy: str,
    checks: list[tuple[str, bool, str]],
) -> None:
    """Write results as a single JUnit <testsuite>."""
    suite = ET.Element(
        "testsuite",
        name=f"tokenops-eval-gate-{policy}",
        tests=str(len(checks)),
        failures=str(sum(1 for _, ok, _ in checks if not ok)),
    )
    for name, ok, detail in checks:
        case = ET.SubElement(suite, "testcase", classname="eval.ci_gate", name=name)
        if not ok:
            failure = ET.SubElement(case, "failure", message=detail)
            failure.text = detail
    ET.ElementTree(suite).write(path, encoding="unicode", xml_declaration=True)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)

    parser = argparse.ArgumentParser(prog="python -m eval.ci_gate")
    parser.add_argument("--dataset", default="eval/golden_dataset.json")
    parser.add_argument("--policy", default="smart-routing")
    parser.add_argument("--junit-out", default="eval-results.xml")
    parser.add_argument(
        "--store",
        action="store_true",
        help="Persist the run to eval_runs (requires DATABASE_URL reachable)",
    )
    args = parser.parse_args(argv)

    dataset = experiments.load_dataset(args.dataset)
    exp = experiments.run_routing_experiment(dataset, args.policy)

    checks: list[tuple[str, bool, str]] = []

    quality_ok = exp.avg_quality >= QUALITY_THRESHOLD
    checks.append(
        (
            "avg_routing_quality",
            quality_ok,
            f"avg_quality={exp.avg_quality:.4f} threshold={QUALITY_THRESHOLD}",
        )
    )

    # Cost regression vs last passing baseline — best-effort: a missing
    # baseline or unreachable DB skips the check rather than failing CI.
    baseline: dict[str, float] | None = None
    try:
        baseline = experiments.fetch_baseline(args.policy)
    except Exception as exc:
        logger.warning("baseline fetch failed — skipping cost check", extra={"error": str(exc)})

    if baseline is not None and baseline["avg_cost"] > 0:
        regression = (exp.avg_cost_usd - baseline["avg_cost"]) / baseline["avg_cost"]
        cost_ok = regression <= COST_REGRESSION_PCT
        checks.append(
            (
                "cost_regression_vs_baseline",
                cost_ok,
                f"avg_cost={exp.avg_cost_usd:.6f} baseline={baseline['avg_cost']:.6f} "
                f"regression={regression:.1%} limit={COST_REGRESSION_PCT:.0%}",
            )
        )
    else:
        logger.info("no baseline available — cost regression check skipped")

    passed = all(ok for _, ok, _ in checks)

    _write_junit(Path(args.junit_out), args.policy, checks)

    if args.store:
        try:
            experiments.store_run(exp, passed=passed)
        except Exception as exc:
            logger.warning("eval_runs insert failed", extra={"error": str(exc)})

    for name, ok, detail in checks:
        status = "PASS" if ok else "FAIL"
        sys.stdout.write(f"[{status}] {name}: {detail}\n")

    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
