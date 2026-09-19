import type { Report } from "@/lib/types";

interface Props {
  report: Report;
}

function formatDuration(seconds: number): string {
  const whole = Math.round(seconds);
  const minutes = Math.floor(whole / 60);
  return minutes ? `${minutes}m ${whole % 60}s` : `${whole}s`;
}

function formatTokens(count: number): string {
  return count >= 1000 ? `${(count / 1000).toFixed(1)}k` : String(count);
}

/**
 * What the run cost, pinned under the rule list.
 *
 * Partly it fills space the rule list leaves empty on a page with two
 * violations. Mostly it is the answer to the first question anyone technical
 * asks about an agent -- how many model calls, how long, how much -- and
 * having it on screen beats saying it out loud.
 */
export default function RunFacts({ report }: Props) {
  const tokens = report.tokens;

  const facts: { label: string; value: string }[] = [
    { label: "Time", value: formatDuration(report.elapsed_seconds) },
    { label: "Model calls", value: String(tokens?.calls ?? 0) },
    {
      label: "Tokens",
      value: `${formatTokens(tokens?.input ?? 0)} in / ${formatTokens(tokens?.output ?? 0)} out`,
    },
    { label: "Edits applied", value: String(report.accepted_edits.length) },
  ];

  return (
    <section className="run-facts" aria-label="What this run cost">
      <dl>
        {facts.map((fact) => (
          <div key={fact.label}>
            <dt>{fact.label}</dt>
            <dd>{fact.value}</dd>
          </div>
        ))}
      </dl>
      <p className="run-facts-note">
        Every edit was applied to a private copy. The live site is untouched.
      </p>
    </section>
  );
}