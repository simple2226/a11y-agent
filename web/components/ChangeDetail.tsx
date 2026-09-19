import type { RuleRow } from "@/lib/types";
import { labelFor } from "@/lib/rules";

interface Props {
  row: RuleRow | null;
}

function describeArgs(op: string, args: Record<string, unknown>): string {
  if (op === "set_attribute") return `${args.name}="${args.value}"`;
  if (op === "remove_attribute") return `remove ${args.name}`;
  if (op === "set_text") return `text → "${args.value}"`;
  if (op === "add_css_rule") return String(args.rule ?? "");
  if (op === "wrap_element") return `wrap in <${args.tag}>`;
  return JSON.stringify(args);
}

export default function ChangeDetail({ row }: Props) {
  if (!row) {
    return (
      <div className="detail">
        <p className="help">Select a rule to see what the agent changed and why.</p>
      </div>
    );
  }

  return (
    <div className="detail">
      <h2>{row.ruleId}</h2>
      <p className="help">
        {labelFor(row.outcome)}. {row.help}.{" "}
        {row.helpUrl ? (
          <a href={row.helpUrl} target="_blank" rel="noreferrer">
            Read the rule
          </a>
        ) : null}
      </p>

      {row.edits.map((edit, index) => (
        <div className="change" key={`${edit.selector}-${edit.op}-${index}`}>
          <div className="change-head">
            <span className="change-op">{edit.op}</span>
            <span className="change-selector">{edit.selector}</span>
            <span className="change-rationale">{edit.rationale}</span>
          </div>
          <div className="snippets">
            <pre className="snippet" data-side="before">
              {edit.selector}
            </pre>
            <pre className="snippet" data-side="after">
              {describeArgs(edit.op, edit.args)}
            </pre>
          </div>
        </div>
      ))}

      {row.deferred.map((item, index) => (
        <div className="deferred-note" key={`${item.selector}-${index}`}>
          <code>{item.selector}</code>
          <div>{item.reason}</div>
        </div>
      ))}

      {row.edits.length === 0 && row.deferred.length === 0 ? (
        <p className="help">
          The agent made no change for this rule. It is still failing on{" "}
          {row.nodesAfter} node{row.nodesAfter === 1 ? "" : "s"}.
        </p>
      ) : null}
    </div>
  );
}
