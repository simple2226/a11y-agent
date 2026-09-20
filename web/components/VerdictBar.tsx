import type { Report } from "@/lib/types";
import type { RuleRow } from "@/lib/types";

interface Props {
  report: Report;
  rows: RuleRow[];
}

/**
 * The result in one line. The two scores are the evidence, the counts explain
 * how they got there -- including the ones that did not go our way.
 */
export default function VerdictBar({ report, rows }: Props) {
  const delta = report.score_after - report.score_before;
  const counts = {
    fixed: rows.filter((row) => row.outcome === "fixed").length,
    partial: rows.filter((row) => row.outcome === "partial").length,
    deferred: rows.filter((row) => row.outcome === "deferred").length,
    unfixed: rows.filter((row) => row.outcome === "unfixed").length,
    reverted: rows.filter((row) => row.outcome === "reverted").length,
  };

  // Elements repaired, not rules cleared.
  //
  // The score caps each rule's penalty at three nodes, so a page where one rule
  // fails on forty elements can have thirty of them fixed and still score the
  // same. That is a real limitation of the formula, and hiding it would make a
  // productive run look like a no-op. This number always moves when work
  // happened, whether or not the score did.
  const nodesBefore = rows.reduce((total, row) => total + row.nodesBefore, 0);
  const nodesAfter = rows.reduce((total, row) => total + row.nodesAfter, 0);
  const nodesFixed = Math.max(0, nodesBefore - nodesAfter);

  const stats: { label: string; value: number; tone?: string }[] = [
    { label: "rules fixed", value: counts.fixed, tone: "pass" },
    { label: "partly fixed", value: counts.partial },
    { label: "left for a person", value: counts.deferred, tone: "defer" },
    { label: "not fixed", value: counts.unfixed, tone: "fail" },
    { label: "reverted", value: counts.reverted, tone: "defer" },
  ].filter((stat) => stat.value > 0);

  return (
    <div className="verdict-bar">
      <p className="verdict-scores">
        <span className="verdict-score before">
          {report.score_before}
          <span className="visually-hidden"> out of 100 before</span>
        </span>
        <span className="verdict-arrow" aria-hidden="true">
          →
        </span>
        <span className="verdict-score after">
          {report.score_after}
          <span className="visually-hidden"> out of 100 after</span>
        </span>
        <span className="verdict-delta" data-direction={delta >= 0 ? "up" : "down"}>
          {delta >= 0 ? "+" : ""}
          {delta}
        </span>
      </p>

      <dl className="verdict-stats">
        {stats.map((stat) => (
          <div className="verdict-stat" key={stat.label} data-tone={stat.tone ?? "neutral"}>
            <dt>{stat.label}</dt>
            <dd>{stat.value}</dd>
          </div>
        ))}
        {nodesFixed > 0 ? (
          <div className="verdict-stat" data-tone="pass">
            <dt>elements repaired</dt>
            <dd>
              {nodesFixed}
              <span className="verdict-stat-of"> of {nodesBefore}</span>
            </dd>
          </div>
        ) : null}
        <div className="verdict-stat" data-tone="neutral">
          <dt>edits applied</dt>
          <dd>{report.accepted_edits.length}</dd>
        </div>
      </dl>
    </div>
  );
}

// import type { Report } from "@/lib/types";
// import type { RuleRow } from "@/lib/types";

// interface Props {
//   report: Report;
//   rows: RuleRow[];
// }

// /**
//  * The result in one line. The two scores are the evidence, the counts explain
//  * how they got there -- including the ones that did not go our way.
//  */
// export default function VerdictBar({ report, rows }: Props) {
//   const delta = report.score_after - report.score_before;
//   const counts = {
//     fixed: rows.filter((row) => row.outcome === "fixed").length,
//     partial: rows.filter((row) => row.outcome === "partial").length,
//     deferred: rows.filter((row) => row.outcome === "deferred").length,
//     unfixed: rows.filter((row) => row.outcome === "unfixed").length,
//     reverted: rows.filter((row) => row.outcome === "reverted").length,
//   };

//   const stats: { label: string; value: number; tone?: string }[] = [
//     { label: "rules fixed", value: counts.fixed, tone: "pass" },
//     { label: "partly fixed", value: counts.partial },
//     { label: "left for a person", value: counts.deferred, tone: "defer" },
//     { label: "not fixed", value: counts.unfixed, tone: "fail" },
//     { label: "reverted", value: counts.reverted, tone: "defer" },
//   ].filter((stat) => stat.value > 0);

//   return (
//     <div className="verdict-bar">
//       <p className="verdict-scores">
//         <span className="verdict-score before">
//           {report.score_before}
//           <span className="visually-hidden"> out of 100 before</span>
//         </span>
//         <span className="verdict-arrow" aria-hidden="true">
//           →
//         </span>
//         <span className="verdict-score after">
//           {report.score_after}
//           <span className="visually-hidden"> out of 100 after</span>
//         </span>
//         <span className="verdict-delta" data-direction={delta >= 0 ? "up" : "down"}>
//           {delta >= 0 ? "+" : ""}
//           {delta}
//         </span>
//       </p>

//       <dl className="verdict-stats">
//         {stats.map((stat) => (
//           <div className="verdict-stat" key={stat.label} data-tone={stat.tone ?? "neutral"}>
//             <dt>{stat.label}</dt>
//             <dd>{stat.value}</dd>
//           </div>
//         ))}
//         <div className="verdict-stat" data-tone="neutral">
//           <dt>edits applied</dt>
//           <dd>{report.accepted_edits.length}</dd>
//         </div>
//       </dl>
//     </div>
//   );
// }
