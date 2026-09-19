# """
# A transparent, reproducible accessibility score computed from axe-core output.

# We deliberately do NOT use the Lighthouse score. Lighthouse would have to run
# against our mirrored copy anyway, so it would not match what a judge sees on the
# live site either -- and it adds a whole Node toolchain to the container. A score
# we define, document, and publish the formula for is more defensible.

# Formula:
#     penalty = sum over violations of (impact_weight * affected_node_count)
#     score   = max(0, 100 - min(100, penalty))

# Node count is capped per violation so that one rule failing on 400 nodes cannot
# by itself zero out the page and hide improvements elsewhere.
# """

# from __future__ import annotations

# from dataclasses import dataclass

# IMPACT_WEIGHT = {
#     "critical": 10,
#     "serious": 7,
#     "moderate": 3,
#     "minor": 1,
# }

# MAX_COUNTED_NODES_PER_VIOLATION = 10

# UNKNOWN_IMPACT_WEIGHT = 3


# def violation_penalty(violation: dict) -> int:
#     weight = IMPACT_WEIGHT.get(violation.get("impact") or "", UNKNOWN_IMPACT_WEIGHT)
#     node_count = min(len(violation.get("nodes", [])), MAX_COUNTED_NODES_PER_VIOLATION)
#     return weight * node_count


# def score_from_violations(violations: list[dict]) -> int:
#     total_penalty = sum(violation_penalty(violation) for violation in violations)
#     return max(0, 100 - min(100, total_penalty))


# @dataclass
# class ScoreDelta:
#     score_before: int
#     score_after: int
#     fixed_rules: list[str]
#     introduced_rules: list[str]
#     partially_fixed: list[dict]
#     unchanged_rules: list[str]

#     @property
#     def improved(self) -> bool:
#         return self.score_after > self.score_before

#     @property
#     def regressed(self) -> bool:
#         return bool(self.introduced_rules) or self.score_after < self.score_before

#     def summary_line(self) -> str:
#         return (
#             f"{self.score_before} -> {self.score_after} | "
#             f"fixed={len(self.fixed_rules)} "
#             f"partial={len(self.partially_fixed)} "
#             f"unchanged={len(self.unchanged_rules)} "
#             f"introduced={len(self.introduced_rules)}"
#         )


# def _node_counts_by_rule(violations: list[dict]) -> dict[str, int]:
#     return {violation["id"]: len(violation.get("nodes", [])) for violation in violations}


# def compare(before_violations: list[dict], after_violations: list[dict]) -> ScoreDelta:
#     before_counts = _node_counts_by_rule(before_violations)
#     after_counts = _node_counts_by_rule(after_violations)

#     fixed_rules = sorted(rule for rule in before_counts if rule not in after_counts)
#     introduced_rules = sorted(rule for rule in after_counts if rule not in before_counts)

#     partially_fixed = []
#     unchanged_rules = []
#     for rule, before_count in before_counts.items():
#         if rule not in after_counts:
#             continue
#         after_count = after_counts[rule]
#         if after_count < before_count:
#             partially_fixed.append(
#                 {"rule": rule, "before": before_count, "after": after_count}
#             )
#         else:
#             unchanged_rules.append(rule)

#     return ScoreDelta(
#         score_before=score_from_violations(before_violations),
#         score_after=score_from_violations(after_violations),
#         fixed_rules=fixed_rules,
#         introduced_rules=introduced_rules,
#         partially_fixed=sorted(partially_fixed, key=lambda item: item["rule"]),
#         unchanged_rules=sorted(unchanged_rules),
#     )


# """
# A transparent, reproducible accessibility score computed from axe-core output.

# We deliberately do NOT use the Lighthouse score. Lighthouse would have to run
# against our mirrored copy anyway, so it would not match what a judge sees on the
# live site either -- and it adds a whole Node toolchain to the container. A score
# we define, document, and publish the formula for is more defensible.

# Formula:
#     penalty = sum over violations of (impact_weight * affected_node_count)
#     score   = max(0, 100 - min(100, penalty))

# Node count is capped per violation so that one rule failing on 400 nodes cannot
# by itself zero out the page and hide improvements elsewhere.
# """

# from __future__ import annotations

# from dataclasses import dataclass

# IMPACT_WEIGHT = {
#     "critical": 10,
#     "serious": 7,
#     "moderate": 3,
#     "minor": 1,
# }

# MAX_COUNTED_NODES_PER_VIOLATION = 10

# UNKNOWN_IMPACT_WEIGHT = 3


# def violation_penalty(violation: dict) -> int:
#     weight = IMPACT_WEIGHT.get(violation.get("impact") or "", UNKNOWN_IMPACT_WEIGHT)
#     node_count = min(len(violation.get("nodes", [])), MAX_COUNTED_NODES_PER_VIOLATION)
#     return weight * node_count


# def score_from_violations(violations: list[dict]) -> int:
#     total_penalty = sum(violation_penalty(violation) for violation in violations)
#     return max(0, 100 - min(100, total_penalty))


# @dataclass
# class ScoreDelta:
#     score_before: int
#     score_after: int
#     fixed_rules: list[str]
#     introduced_rules: list[str]
#     partially_fixed: list[dict]
#     unchanged_rules: list[str]

#     @property
#     def improved(self) -> bool:
#         return self.score_after > self.score_before

#     @property
#     def regressed(self) -> bool:
#         return bool(self.introduced_rules) or self.score_after < self.score_before

#     def summary_line(self) -> str:
#         return (
#             f"{self.score_before} -> {self.score_after} | "
#             f"fixed={len(self.fixed_rules)} "
#             f"partial={len(self.partially_fixed)} "
#             f"unchanged={len(self.unchanged_rules)} "
#             f"introduced={len(self.introduced_rules)}"
#         )


# def _node_counts_by_rule(violations: list[dict]) -> dict[str, int]:
#     return {violation["id"]: len(violation.get("nodes", [])) for violation in violations}


# def compare(before_violations: list[dict], after_violations: list[dict]) -> ScoreDelta:
#     before_counts = _node_counts_by_rule(before_violations)
#     after_counts = _node_counts_by_rule(after_violations)

#     fixed_rules = sorted(rule for rule in before_counts if rule not in after_counts)
#     introduced_rules = sorted(rule for rule in after_counts if rule not in before_counts)

#     partially_fixed = []
#     unchanged_rules = []
#     for rule, before_count in before_counts.items():
#         if rule not in after_counts:
#             continue
#         after_count = after_counts[rule]
#         if after_count < before_count:
#             partially_fixed.append(
#                 {"rule": rule, "before": before_count, "after": after_count}
#             )
#         else:
#             unchanged_rules.append(rule)

#     return ScoreDelta(
#         score_before=score_from_violations(before_violations),
#         score_after=score_from_violations(after_violations),
#         fixed_rules=fixed_rules,
#         introduced_rules=introduced_rules,
#         partially_fixed=sorted(partially_fixed, key=lambda item: item["rule"]),
#         unchanged_rules=sorted(unchanged_rules),
#     )


"""
A transparent, reproducible accessibility score computed from axe-core output.

We deliberately do NOT use the Lighthouse score. Lighthouse would have to run
against our mirrored copy anyway, so it would not match what a judge sees on the
live site either -- and it adds a whole Node toolchain to the container. A score
we define, document, and publish the formula for is more defensible.

Formula:
    penalty = sum over violations of (impact_weight * affected_node_count)
    score   = max(0, 100 - min(100, penalty))

Node count is capped per violation so that one rule failing on 400 nodes cannot
by itself zero out the page and hide improvements elsewhere.
"""

from __future__ import annotations

from dataclasses import dataclass

IMPACT_WEIGHT = {
    "critical": 10,
    "serious": 7,
    "moderate": 3,
    "minor": 1,
}

# Each rule contributes at most weight x this many nodes. Without a cap, one
# rule failing on 40 nodes costs 400 penalty, the total clamps at 100, and every
# page scores 0 -- which hides all improvement and reads as "measurement failed".
MAX_COUNTED_NODES_PER_VIOLATION = 3

UNKNOWN_IMPACT_WEIGHT = 3


def violation_penalty(violation: dict) -> int:
    weight = IMPACT_WEIGHT.get(violation.get("impact") or "", UNKNOWN_IMPACT_WEIGHT)
    reported_nodes = violation.get("totalNodes", len(violation.get("nodes", [])))
    node_count = min(reported_nodes, MAX_COUNTED_NODES_PER_VIOLATION)
    return weight * node_count


def score_from_violations(violations: list[dict]) -> int:
    total_penalty = sum(violation_penalty(violation) for violation in violations)
    return max(0, 100 - min(100, total_penalty))


@dataclass
class ScoreDelta:
    score_before: int
    score_after: int
    fixed_rules: list[str]
    introduced_rules: list[str]
    partially_fixed: list[dict]
    unchanged_rules: list[str]

    @property
    def improved(self) -> bool:
        return self.score_after > self.score_before

    @property
    def regressed(self) -> bool:
        return bool(self.introduced_rules) or self.score_after < self.score_before

    def summary_line(self) -> str:
        return (
            f"{self.score_before} -> {self.score_after} | "
            f"fixed={len(self.fixed_rules)} "
            f"partial={len(self.partially_fixed)} "
            f"unchanged={len(self.unchanged_rules)} "
            f"introduced={len(self.introduced_rules)}"
        )


def _node_counts_by_rule(violations: list[dict]) -> dict[str, int]:
    return {violation["id"]: len(violation.get("nodes", [])) for violation in violations}


def compare(before_violations: list[dict], after_violations: list[dict]) -> ScoreDelta:
    before_counts = _node_counts_by_rule(before_violations)
    after_counts = _node_counts_by_rule(after_violations)

    fixed_rules = sorted(rule for rule in before_counts if rule not in after_counts)
    introduced_rules = sorted(rule for rule in after_counts if rule not in before_counts)

    partially_fixed = []
    unchanged_rules = []
    for rule, before_count in before_counts.items():
        if rule not in after_counts:
            continue
        after_count = after_counts[rule]
        if after_count < before_count:
            partially_fixed.append(
                {"rule": rule, "before": before_count, "after": after_count}
            )
        else:
            unchanged_rules.append(rule)

    return ScoreDelta(
        score_before=score_from_violations(before_violations),
        score_after=score_from_violations(after_violations),
        fixed_rules=fixed_rules,
        introduced_rules=introduced_rules,
        partially_fixed=sorted(partially_fixed, key=lambda item: item["rule"]),
        unchanged_rules=sorted(unchanged_rules),
    )