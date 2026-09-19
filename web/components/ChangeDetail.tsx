import type { AppliedEdit, RuleRow } from "@/lib/types";
import { labelFor } from "@/lib/rules";

interface Props {
  row: RuleRow | null;
}

const OP_LABEL: Record<string, string> = {
  set_attribute: "set attribute",
  remove_attribute: "remove attribute",
  set_text: "replace text",
  wrap_element: "wrap element",
  insert_before: "insert before",
  insert_after: "insert after",
  add_css_rule: "add CSS rule",
};

/** What the edit did, in words, for edits with no markup to diff. */
function describeChange(edit: AppliedEdit): string {
  const args = edit.args ?? {};
  if (edit.op === "set_attribute") return `${args.name}="${args.value}"`;
  if (edit.op === "remove_attribute") return `removed ${args.name}`;
  if (edit.op === "set_text") return String(args.value ?? "");
  if (edit.op === "add_css_rule") return String(args.rule ?? "");
  if (edit.op === "wrap_element") return `<${args.tag}>`;
  return JSON.stringify(args);
}

function EditCard({ edit }: { edit: AppliedEdit }) {
  const before = edit.before_snippets ?? [];
  const after = edit.after_snippets ?? [];
  const hasDiff = before.length > 0 && after.length > 0;
  const extraNodes = (edit.matched_nodes ?? 1) - before.length;

  return (
    <article className="change">
      <header className="change-head">
        <span className="change-op">{OP_LABEL[edit.op] ?? edit.op}</span>
        <code className="change-selector">{edit.selector}</code>
        {edit.matched_nodes && edit.matched_nodes > 1 ? (
          <span className="change-count">{edit.matched_nodes} nodes</span>
        ) : null}
      </header>

      {hasDiff ? (
        <>
          {before.map((snippet, index) => (
            <div className="snippets" key={index}>
              <pre className="snippet" data-side="before">
                <span className="snippet-label" aria-hidden="true">
                  −
                </span>
                <span className="visually-hidden">Before: </span>
                {snippet}
              </pre>
              <pre className="snippet" data-side="after">
                <span className="snippet-label" aria-hidden="true">
                  +
                </span>
                <span className="visually-hidden">After: </span>
                {after[index] ?? ""}
              </pre>
            </div>
          ))}
          {extraNodes > 0 ? (
            <p className="change-more">
              and {extraNodes} more node{extraNodes === 1 ? "" : "s"} changed the same way
            </p>
          ) : null}
        </>
      ) : (
        <div className="snippets">
          <pre className="snippet" data-side="after">
            <span className="snippet-label" aria-hidden="true">
              +
            </span>
            {describeChange(edit)}
          </pre>
        </div>
      )}

      {edit.rationale ? <p className="change-rationale">{edit.rationale}</p> : null}
    </article>
  );
}

export default function ChangeDetail({ row }: Props) {
  if (!row) {
    return (
      <div className="detail">
        <p className="detail-empty">Select a rule to see what the agent changed, and why.</p>
      </div>
    );
  }

  const nothingHappened = row.edits.length === 0 && row.deferred.length === 0;

  return (
    <div className="detail">
      <div className="detail-head">
        <h2>{row.ruleId}</h2>
        <span className="detail-outcome" data-outcome={row.outcome}>
          {labelFor(row.outcome)}
        </span>
      </div>

      <p className="detail-help">
        {row.help}.{" "}
        {row.helpUrl ? (
          <a href={row.helpUrl} target="_blank" rel="noreferrer">
            Read the rule
          </a>
        ) : null}
      </p>

      {row.edits.map((edit, index) => (
        <EditCard edit={edit} key={`${edit.selector}-${edit.op}-${index}`} />
      ))}

      {row.deferred.length ? (
        <section className="deferred-block">
          <h3 className="deferred-title">
            Left for a person ({row.deferred.length})
          </h3>
          {row.deferred.map((item, index) => (
            <div className="deferred-note" key={`${item.selector}-${index}`}>
              <code>{item.selector}</code>
              <p>{item.reason}</p>
            </div>
          ))}
        </section>
      ) : null}

      {nothingHappened ? (
        <p className="detail-empty">
          The agent changed nothing here. Still failing on {row.nodesAfter} node
          {row.nodesAfter === 1 ? "" : "s"}.
        </p>
      ) : null}
    </div>
  );
}
