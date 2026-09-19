import type { AxeViolation, Report, RuleOutcome, RuleRow } from "./types";

const OUTCOME_MARKER: Record<RuleOutcome, string> = {
  fixed: "[+]",
  partial: "[~]",
  unfixed: "[!]",
  deferred: "[?]",
  introduced: "[x]",
  reverted: "[<]",
};

const OUTCOME_LABEL: Record<RuleOutcome, string> = {
  fixed: "Fixed",
  partial: "Partly fixed",
  unfixed: "Not fixed",
  deferred: "Needs a person",
  introduced: "Introduced by the agent",
  reverted: "Reverted — the fix made things worse",
};

const IMPACT_ORDER = ["critical", "serious", "moderate", "minor"];

export function markerFor(outcome: RuleOutcome): string {
  return OUTCOME_MARKER[outcome];
}

export function labelFor(outcome: RuleOutcome): string {
  return OUTCOME_LABEL[outcome];
}

function selectorsFrom(violation: AxeViolation | undefined): string[] {
  if (!violation) return [];
  return violation.nodes
    .map((node) => node.target?.[0])
    .filter((selector): selector is string => Boolean(selector));
}

/**
 * Joins the report's several flat lists into one row per axe rule, so the UI
 * never has to cross-reference four arrays while rendering.
 */
export function buildRuleRows(report: Report): RuleRow[] {
  const beforeByRule = new Map(report.original_violations.map((v) => [v.id, v]));
  const afterByRule = new Map(report.final_violations.map((v) => [v.id, v]));
  const partialByRule = new Map(report.partially_fixed.map((p) => [p.rule, p]));

  // The pipeline now labels a rule "deferred" when every node still failing it
  // was deliberately handed to a human. Fall back to inferring it from the
  // deferred items for reports produced before that existed.
  const revertedRuleIds = new Set(report.reverted_rules ?? []);
  const deferredRuleIds = new Set(
    report.deferred_rules ?? report.deferred_items.map((item) => item.violation_id),
  );
  const rows: RuleRow[] = [];

  const allRuleIds = new Set([...beforeByRule.keys(), ...afterByRule.keys()]);

  for (const ruleId of allRuleIds) {
    const before = beforeByRule.get(ruleId);
    const after = afterByRule.get(ruleId);

    let outcome: RuleOutcome;
    if (revertedRuleIds.has(ruleId)) {
      outcome = "reverted";
    } else if (!before && after) {
      outcome = "introduced";
    } else if (before && !after) {
      outcome = "fixed";
    } else if (partialByRule.has(ruleId)) {
      outcome = "partial";
    } else if (deferredRuleIds.has(ruleId)) {
      outcome = "deferred";
    } else {
      outcome = "unfixed";
    }

    const source = before ?? after!;

    rows.push({
      ruleId,
      impact: source.impact,
      help: source.help,
      helpUrl: source.helpUrl,
      outcome,
      nodesBefore: before?.totalNodes ?? 0,
      nodesAfter: after?.totalNodes ?? 0,
      // Highlight the nodes that were failing originally -- that is what the
      // viewer wants to see marked in both frames.
      selectors: selectorsFrom(before ?? after),
      edits: report.accepted_edits.filter((edit) => edit.violation_id === ruleId),
      deferred: report.deferred_items.filter((item) => item.violation_id === ruleId),
    });
  }

  // Introduced regressions first (they are the honest bad news), then by impact,
  // then by how many nodes were affected.
  const outcomeRank: Record<RuleOutcome, number> = {
    introduced: 0,
    reverted: 1,
    unfixed: 2,
    partial: 3,
    deferred: 4,
    fixed: 5,
  };

  return rows.sort((left, right) => {
    if (outcomeRank[left.outcome] !== outcomeRank[right.outcome]) {
      return outcomeRank[left.outcome] - outcomeRank[right.outcome];
    }
    const impactDelta =
      IMPACT_ORDER.indexOf(left.impact ?? "minor") - IMPACT_ORDER.indexOf(right.impact ?? "minor");
    if (impactDelta !== 0) return impactDelta;
    return right.nodesBefore - left.nodesBefore;
  });
}
