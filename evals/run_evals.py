# #!/usr/bin/env python3
# """
# Run the full pipeline over every frozen fixture and print a results table.

#     python evals/run_evals.py
#     python evals/run_evals.py --no-agent     # audit-only baseline scores

# This is the four seconds of terminal output that goes in the demo video. Report
# what actually happened. A table showing 9 of 15 improved with two regressions
# and a reason for each is worth more than a fabricated clean sweep.
# """

# from __future__ import annotations

# import argparse
# import json
# import sys
# import time
# from pathlib import Path

# sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# from audit.runner import audit_html
# from audit.scorer import compare, score_from_violations

# FIXTURES_DIRECTORY = Path(__file__).resolve().parent / "fixtures"
# RESULTS_DIRECTORY = Path(__file__).resolve().parent / "results"


# def evaluate_fixture(fixture_path: Path, use_agent: bool) -> dict:
#     html_text = fixture_path.read_text(encoding="utf-8")

#     if not use_agent:
#         result = audit_html(html_text, take_screenshot=False)
#         return {
#             "fixture": fixture_path.name,
#             "score_before": score_from_violations(result.violations),
#             "score_after": None,
#             "rules_failing": len(result.violations),
#         }

#     from agent.graph import build_graph, initial_state

#     graph = build_graph()
#     started_at = time.time()
#     final_state = graph.invoke(
#         initial_state(
#             run_id=fixture_path.stem,
#             page_url=f"file://{fixture_path}",
#             page_title=fixture_path.stem,
#             original_html=html_text,
#         ),
#         config={"recursion_limit": 100},
#     )
#     elapsed = time.time() - started_at

#     delta = compare(final_state["original_violations"], final_state["final_violations"])
#     usage = final_state.get("token_usage")

#     return {
#         "fixture": fixture_path.name,
#         "score_before": delta.score_before,
#         "score_after": delta.score_after,
#         "fixed": len(delta.fixed_rules),
#         "partial": len(delta.partially_fixed),
#         "unchanged": len(delta.unchanged_rules),
#         "introduced": len(delta.introduced_rules),
#         "introduced_rules": delta.introduced_rules,
#         "unfixed_rules": final_state.get("unfixed_rules", []),
#         "deferred": len(final_state.get("deferred_items", [])),
#         "edits": len(final_state.get("accepted_edits", [])),
#         "tokens_in": usage.input_tokens if usage else 0,
#         "tokens_out": usage.output_tokens if usage else 0,
#         "seconds": round(elapsed, 1),
#     }


# def print_table(rows: list[dict], use_agent: bool) -> None:
#     if not use_agent:
#         print(f"\n{'fixture':<34} {'score':>6} {'rules':>6}")
#         print("-" * 50)
#         for row in rows:
#             print(f"{row['fixture']:<34} {row['score_before']:>6} {row['rules_failing']:>6}")
#         return

#     header = (f"{'fixture':<28} {'before':>6} {'after':>6} {'Δ':>5} "
#               f"{'fix':>4} {'part':>5} {'new':>4} {'defer':>6} {'sec':>6}")
#     print("\n" + header)
#     print("-" * len(header))

#     improved = regressed = unchanged = 0
#     for row in rows:
#         delta_value = row["score_after"] - row["score_before"]
#         if row["introduced"] or delta_value < 0:
#             regressed += 1
#         elif delta_value > 0:
#             improved += 1
#         else:
#             unchanged += 1

#         print(
#             f"{row['fixture']:<28} {row['score_before']:>6} {row['score_after']:>6} "
#             f"{delta_value:>+5} {row['fixed']:>4} {row['partial']:>5} "
#             f"{row['introduced']:>4} {row['deferred']:>6} {row['seconds']:>6}"
#         )

#     print("-" * len(header))
#     print(f"{len(rows)} fixtures: {improved} improved, {regressed} regressed, {unchanged} unchanged")

#     total_in = sum(row["tokens_in"] for row in rows)
#     total_out = sum(row["tokens_out"] for row in rows)
#     print(f"tokens: {total_in} in / {total_out} out across {len(rows)} pages")

#     for row in rows:
#         if row["introduced_rules"]:
#             print(f"  REGRESSION {row['fixture']}: introduced {', '.join(row['introduced_rules'])}")
#         if row["unfixed_rules"]:
#             print(f"  UNFIXED    {row['fixture']}: {', '.join(row['unfixed_rules'])}")


# def main() -> int:
#     parser = argparse.ArgumentParser()
#     parser.add_argument("--no-agent", action="store_true")
#     parser.add_argument("--pattern", default="*.html")
#     arguments = parser.parse_args()

#     fixture_paths = sorted(FIXTURES_DIRECTORY.glob(arguments.pattern))
#     if not fixture_paths:
#         print(f"no fixtures in {FIXTURES_DIRECTORY}", file=sys.stderr)
#         return 1

#     rows = []
#     for fixture_path in fixture_paths:
#         print(f"running {fixture_path.name} ...", file=sys.stderr)
#         try:
#             rows.append(evaluate_fixture(fixture_path, use_agent=not arguments.no_agent))
#         except Exception as error:
#             print(f"  FAILED {fixture_path.name}: {error}", file=sys.stderr)

#     print_table(rows, use_agent=not arguments.no_agent)

#     RESULTS_DIRECTORY.mkdir(exist_ok=True)
#     (RESULTS_DIRECTORY / "results.json").write_text(json.dumps(rows, indent=2), encoding="utf-8")
#     return 0


# if __name__ == "__main__":
#     sys.exit(main())


# #!/usr/bin/env python3
# """
# Run the full pipeline over every frozen fixture and print a results table.

#     python evals/run_evals.py
#     python evals/run_evals.py --no-agent     # audit-only baseline scores

# This is the four seconds of terminal output that goes in the demo video. Report
# what actually happened. A table showing 9 of 15 improved with two regressions
# and a reason for each is worth more than a fabricated clean sweep.
# """

# from __future__ import annotations

# import argparse
# import json
# import sys
# import time
# from pathlib import Path

# sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# from audit.runner import audit_html
# from audit.scorer import compare, score_from_violations

# FIXTURES_DIRECTORY = Path(__file__).resolve().parent / "fixtures"
# RESULTS_DIRECTORY = Path(__file__).resolve().parent / "results"


# def evaluate_fixture(fixture_path: Path, use_agent: bool) -> dict:
#     html_text = fixture_path.read_text(encoding="utf-8")

#     if not use_agent:
#         result = audit_html(html_text, take_screenshot=False)
#         return {
#             "fixture": fixture_path.name,
#             "score_before": score_from_violations(result.violations),
#             "score_after": None,
#             "rules_failing": len(result.violations),
#         }

#     from agent.graph import build_graph, initial_state

#     graph = build_graph()
#     started_at = time.time()
#     final_state = graph.invoke(
#         initial_state(
#             run_id=fixture_path.stem,
#             page_url=f"file://{fixture_path}",
#             page_title=fixture_path.stem,
#             original_html=html_text,
#         ),
#         config={"recursion_limit": 100},
#     )
#     elapsed = time.time() - started_at

#     delta = compare(final_state["original_violations"], final_state["final_violations"])
#     usage = final_state.get("token_usage")

#     return {
#         "fixture": fixture_path.name,
#         "score_before": delta.score_before,
#         "score_after": delta.score_after,
#         "fixed": len(delta.fixed_rules),
#         "partial": len(delta.partially_fixed),
#         "unchanged": len(delta.unchanged_rules),
#         "introduced": len(delta.introduced_rules),
#         "introduced_rules": delta.introduced_rules,
#         "unfixed_rules": final_state.get("unfixed_rules", []),
#         "deferred": len(final_state.get("deferred_items", [])),
#         "edits": len(final_state.get("accepted_edits", [])),
#         "tokens_in": usage.input_tokens if usage else 0,
#         "tokens_out": usage.output_tokens if usage else 0,
#         "seconds": round(elapsed, 1),
#     }


# def print_table(rows: list[dict], use_agent: bool) -> None:
#     if not use_agent:
#         print(f"\n{'fixture':<34} {'score':>6} {'rules':>6}")
#         print("-" * 50)
#         for row in rows:
#             print(f"{row['fixture']:<34} {row['score_before']:>6} {row['rules_failing']:>6}")
#         return

#     header = (f"{'fixture':<28} {'before':>6} {'after':>6} {'Δ':>5} "
#               f"{'fix':>4} {'part':>5} {'new':>4} {'defer':>6} {'sec':>6}")
#     print("\n" + header)
#     print("-" * len(header))

#     improved = regressed = unchanged = 0
#     for row in rows:
#         delta_value = row["score_after"] - row["score_before"]
#         if row["introduced"] or delta_value < 0:
#             regressed += 1
#         elif delta_value > 0:
#             improved += 1
#         else:
#             unchanged += 1

#         print(
#             f"{row['fixture']:<28} {row['score_before']:>6} {row['score_after']:>6} "
#             f"{delta_value:>+5} {row['fixed']:>4} {row['partial']:>5} "
#             f"{row['introduced']:>4} {row['deferred']:>6} {row['seconds']:>6}"
#         )

#     print("-" * len(header))
#     print(f"{len(rows)} fixtures: {improved} improved, {regressed} regressed, {unchanged} unchanged")

#     total_in = sum(row["tokens_in"] for row in rows)
#     total_out = sum(row["tokens_out"] for row in rows)
#     print(f"tokens: {total_in} in / {total_out} out across {len(rows)} pages")

#     for row in rows:
#         if row["introduced_rules"]:
#             print(f"  REGRESSION {row['fixture']}: introduced {', '.join(row['introduced_rules'])}")
#         if row["unfixed_rules"]:
#             print(f"  UNFIXED    {row['fixture']}: {', '.join(row['unfixed_rules'])}")


# def main() -> int:
#     parser = argparse.ArgumentParser()
#     parser.add_argument("--no-agent", action="store_true")
#     parser.add_argument("--pattern", default="*.html")
#     arguments = parser.parse_args()

#     fixture_paths = sorted(FIXTURES_DIRECTORY.glob(arguments.pattern))
#     if not fixture_paths:
#         print(f"no fixtures in {FIXTURES_DIRECTORY}", file=sys.stderr)
#         return 1

#     rows = []
#     for fixture_path in fixture_paths:
#         print(f"running {fixture_path.name} ...", file=sys.stderr)
#         try:
#             rows.append(evaluate_fixture(fixture_path, use_agent=not arguments.no_agent))
#         except Exception as error:
#             print(f"  FAILED {fixture_path.name}: {error}", file=sys.stderr)

#     print_table(rows, use_agent=not arguments.no_agent)

#     RESULTS_DIRECTORY.mkdir(exist_ok=True)
#     (RESULTS_DIRECTORY / "results.json").write_text(json.dumps(rows, indent=2), encoding="utf-8")
#     return 0


# if __name__ == "__main__":
#     sys.exit(main())


#!/usr/bin/env python3
"""
Run the full pipeline over every frozen fixture and print a results table.

    python evals/run_evals.py
    python evals/run_evals.py --no-agent     # audit-only baseline scores

This is the four seconds of terminal output that goes in the demo video. Report
what actually happened. A table showing 9 of 15 improved with two regressions
and a reason for each is worth more than a fabricated clean sweep.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from audit.runner import audit_html
from audit.scorer import compare, score_from_violations

FIXTURES_DIRECTORY = Path(__file__).resolve().parent / "fixtures"
RESULTS_DIRECTORY = Path(__file__).resolve().parent / "results"


def evaluate_fixture(fixture_path: Path, use_agent: bool) -> dict:
    html_text = fixture_path.read_text(encoding="utf-8")

    if not use_agent:
        result = audit_html(html_text, take_screenshot=False)
        return {
            "fixture": fixture_path.name,
            "score_before": score_from_violations(result.violations),
            "score_after": None,
            "rules_failing": len(result.violations),
        }

    from agent.graph import build_graph, initial_state

    graph = build_graph()
    started_at = time.time()
    final_state = graph.invoke(
        initial_state(
            run_id=fixture_path.stem,
            page_url=f"file://{fixture_path}",
            page_title=fixture_path.stem,
            original_html=html_text,
        ),
        config={"recursion_limit": 100},
    )
    elapsed = time.time() - started_at

    delta = compare(final_state["original_violations"], final_state["final_violations"])
    usage = final_state.get("token_usage")

    return {
        "fixture": fixture_path.name,
        "score_before": delta.score_before,
        "score_after": delta.score_after,
        "fixed": len(delta.fixed_rules),
        "partial": len(delta.partially_fixed),
        "unchanged": len(delta.unchanged_rules),
        "introduced": len(delta.introduced_rules),
        "introduced_rules": delta.introduced_rules,
        "unfixed_rules": final_state.get("unfixed_rules", []),
        "deferred": len(final_state.get("deferred_items", [])),
        "edits": len(final_state.get("accepted_edits", [])),
        "tokens_in": usage.input_tokens if usage else 0,
        "tokens_out": usage.output_tokens if usage else 0,
        "seconds": round(elapsed, 1),
    }


def print_table(rows: list[dict], use_agent: bool) -> None:
    if not use_agent:
        print(f"\n{'fixture':<34} {'score':>6} {'rules':>6}")
        print("-" * 50)
        for row in rows:
            print(f"{row['fixture']:<34} {row['score_before']:>6} {row['rules_failing']:>6}")
        return

    header = (f"{'fixture':<28} {'before':>6} {'after':>6} {'Δ':>5} "
              f"{'fix':>4} {'part':>5} {'new':>4} {'defer':>6} {'sec':>6}")
    print("\n" + header)
    print("-" * len(header))

    improved = regressed = unchanged = 0
    for row in rows:
        delta_value = row["score_after"] - row["score_before"]
        if row["introduced"] or delta_value < 0:
            regressed += 1
        elif delta_value > 0:
            improved += 1
        else:
            unchanged += 1

        print(
            f"{row['fixture']:<28} {row['score_before']:>6} {row['score_after']:>6} "
            f"{delta_value:>+5} {row['fixed']:>4} {row['partial']:>5} "
            f"{row['introduced']:>4} {row['deferred']:>6} {row['seconds']:>6}"
        )

    print("-" * len(header))
    print(f"{len(rows)} fixtures: {improved} improved, {regressed} regressed, {unchanged} unchanged")

    total_in = sum(row["tokens_in"] for row in rows)
    total_out = sum(row["tokens_out"] for row in rows)
    print(f"tokens: {total_in} in / {total_out} out across {len(rows)} pages")

    for row in rows:
        if row["introduced_rules"]:
            print(f"  REGRESSION {row['fixture']}: introduced {', '.join(row['introduced_rules'])}")
        if row["unfixed_rules"]:
            print(f"  UNFIXED    {row['fixture']}: {', '.join(row['unfixed_rules'])}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-agent", action="store_true")
    parser.add_argument("--pattern", default="*.html")
    parser.add_argument(
        "--provider",
        choices=["bedrock", "anthropic", "gemini", "openai", "groq", "openrouter",
                 "ollama", "mock"],
        help="Which model backend to use. Overrides MODEL_PROVIDER for this run.",
    )
    arguments = parser.parse_args()

    if arguments.provider:
        os.environ["MODEL_PROVIDER"] = arguments.provider
        if arguments.provider == "mock":
            os.environ["A11Y_MOCK_MODEL"] = "1"
        else:
            os.environ.pop("A11Y_MOCK_MODEL", None)

    if not arguments.no_agent:
        from agent.model_provider import active_model_label

        print(f"model: {active_model_label()}", file=sys.stderr)

    fixture_paths = sorted(FIXTURES_DIRECTORY.glob(arguments.pattern))
    if not fixture_paths:
        print(f"no fixtures in {FIXTURES_DIRECTORY}", file=sys.stderr)
        return 1

    rows = []
    for fixture_path in fixture_paths:
        print(f"running {fixture_path.name} ...", file=sys.stderr)
        try:
            rows.append(evaluate_fixture(fixture_path, use_agent=not arguments.no_agent))
        except Exception as error:
            print(f"  FAILED {fixture_path.name}: {error}", file=sys.stderr)

    print_table(rows, use_agent=not arguments.no_agent)

    RESULTS_DIRECTORY.mkdir(exist_ok=True)
    (RESULTS_DIRECTORY / "results.json").write_text(json.dumps(rows, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())