"use client";

import type { RuleRow } from "@/lib/types";
import { labelFor, markerFor } from "@/lib/rules";

interface Props {
  rows: RuleRow[];
  selectedRuleId: string | null;
  onSelect: (ruleId: string) => void;
}

function countLabel(row: RuleRow): string {
  if (row.outcome === "fixed") return `${row.nodesBefore} → 0`;
  if (row.outcome === "introduced") return `0 → ${row.nodesAfter}`;
  return `${row.nodesBefore} → ${row.nodesAfter}`;
}

export default function RuleList({ rows, selectedRuleId, onSelect }: Props) {
  return (
    <nav className="rules" aria-label="Accessibility rules found on this page">
      <p className="rules-header">
        {rows.length} rules checked and failing before the run. Select one to see it
        outlined in both frames.
      </p>
      <ul className="rule-list">
        {rows.map((row) => (
          <li key={row.ruleId}>
            <button
              type="button"
              className="rule-button"
              aria-current={row.ruleId === selectedRuleId}
              onClick={() => onSelect(row.ruleId)}
            >
              <span className="marker" data-outcome={row.outcome} aria-hidden="true">
                {markerFor(row.outcome)}
              </span>
              <span className="rule-body">
                <span className="rule-top">
                  <span className="rule-id">{row.ruleId}</span>
                  {row.impact ? (
                    <span className="rule-impact" data-impact={row.impact}>
                      {row.impact}
                    </span>
                  ) : null}
                </span>
                <span className="visually-hidden">. {labelFor(row.outcome)}.</span>
                <span className="rule-help">{row.help}</span>
              </span>
              <span className="rule-count">{countLabel(row)}</span>
            </button>
          </li>
        ))}
      </ul>
    </nav>
  );
}