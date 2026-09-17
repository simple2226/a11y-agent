"""
Group axe violations into work units for the model.

One cluster == one axe rule. Forty missing alt attributes are one LLM call, not
forty. Clusters are ordered by how much score they can recover, so if we run out
of budget we have already fixed the things that matter most.
"""

from __future__ import annotations

from dataclasses import dataclass

from audit.scorer import violation_penalty

# Rules where a generic automated fix is usually wrong, and the model should
# lean on `deferred` rather than inventing content. Surfaced in the prompt.
HIGH_JUDGEMENT_RULES = {
    "image-alt",
    "input-image-alt",
    "area-alt",
    "object-alt",
    "video-caption",
    "audio-caption",
    "frame-title",
}

# Rules we do not attempt at all: fixing them needs the rendered page, computed
# styles, or a design decision that no static DOM edit can make safely.
SKIPPED_RULES = {
    "color-contrast-enhanced",
    "scrollable-region-focusable",
}

MAX_NODES_SENT_PER_CLUSTER = 12


@dataclass
class ViolationCluster:
    rule_id: str
    impact: str
    help_text: str
    description: str
    help_url: str
    total_nodes: int
    sample_nodes: list[dict]
    recoverable_penalty: int

    @property
    def needs_human_judgement(self) -> bool:
        return self.rule_id in HIGH_JUDGEMENT_RULES


def build_clusters(violations: list[dict]) -> list[ViolationCluster]:
    clusters: list[ViolationCluster] = []

    for violation in violations:
        rule_id = violation.get("id", "")
        if rule_id in SKIPPED_RULES:
            continue

        clusters.append(
            ViolationCluster(
                rule_id=rule_id,
                impact=violation.get("impact") or "moderate",
                help_text=violation.get("help") or "",
                description=violation.get("description") or "",
                help_url=violation.get("helpUrl") or "",
                total_nodes=violation.get("totalNodes", len(violation.get("nodes", []))),
                sample_nodes=violation.get("nodes", [])[:MAX_NODES_SENT_PER_CLUSTER],
                recoverable_penalty=violation_penalty(violation),
            )
        )

    clusters.sort(key=lambda cluster: cluster.recoverable_penalty, reverse=True)
    return clusters
