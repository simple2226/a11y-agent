export type EditOperation =
  | "set_attribute"
  | "remove_attribute"
  | "set_text"
  | "wrap_element"
  | "insert_before"
  | "insert_after"
  | "add_css_rule";

export interface AppliedEdit {
  violation_id: string;
  selector: string;
  op: EditOperation;
  args: Record<string, unknown>;
  rationale: string;
}

export interface DeferredItem {
  violation_id: string;
  selector: string;
  reason: string;
}

export interface AxeNode {
  target: string[];
  html: string;
  failureSummary: string;
}

export interface AxeViolation {
  id: string;
  impact: "critical" | "serious" | "moderate" | "minor" | null;
  help: string;
  description: string;
  helpUrl: string;
  totalNodes: number;
  nodes: AxeNode[];
}

export interface ClusterReport {
  rule: string;
  status: "fixed" | "unfixed" | "deferred" | "reverted";
  attempts: number;
}

export interface Report {
  run_id: string;
  page_url: string;
  page_title: string;
  elapsed_seconds: number;
  score_before: number;
  score_after: number;
  fixed_rules: string[];
  partially_fixed: { rule: string; before: number; after: number }[];
  unchanged_rules: string[];
  introduced_rules: string[];
  unfixed_rules: string[];
  deferred_rules?: string[];
  reverted_rules?: string[];
  cluster_reports: ClusterReport[];
  accepted_edits: AppliedEdit[];
  deferred_items: DeferredItem[];
  original_violations: AxeViolation[];
  final_violations: AxeViolation[];
  tokens: { input: number; output: number; calls: number };
}

/** What the UI renders per rule, after joining the report's several lists. */
export type RuleOutcome =
  | "fixed"
  | "partial"
  | "unfixed"
  | "deferred"
  | "introduced"
  | "reverted";

export interface RuleRow {
  ruleId: string;
  impact: AxeViolation["impact"];
  help: string;
  helpUrl: string;
  outcome: RuleOutcome;
  nodesBefore: number;
  nodesAfter: number;
  selectors: string[];
  edits: AppliedEdit[];
  deferred: DeferredItem[];
}
