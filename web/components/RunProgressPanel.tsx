"use client";

import { useEffect, useRef } from "react";
import type { RunSummary } from "@/lib/api";

interface Props {
  run: RunSummary | null;
  url: string | null;
}

/**
 * What the agent is doing right now.
 *
 * A run takes minutes, and the only honest way to show that is to show the
 * work. The Lambda writes each graph node's log line to DynamoDB as it happens
 * and this renders them as they arrive.
 */
export default function RunProgressPanel({ run, url }: Props) {
  const logEndRef = useRef<HTMLDivElement | null>(null);
  const page = run?.pages?.[0];
  const progress = page?.progress ?? null;
  const lines = progress?.log ?? [];

  useEffect(() => {
    logEndRef.current?.scrollIntoView({ block: "end" });
  }, [lines.length]);

  const total = progress?.clustersTotal ?? 0;
  const done = progress?.clustersDone ?? 0;
  const percent = total > 0 ? Math.round((done / total) * 100) : null;

  return (
    <main className="progress-panel">
      <div className="progress-head">
        <span className="spinner" aria-hidden="true" />
        <div>
          <h2 className="progress-title">
            Auditing {url ? <code>{url}</code> : "the page"}
          </h2>
          <p className="progress-phase" aria-live="polite">
            {progress?.phase ?? "Starting up — Lambda cold start takes a few seconds"}
          </p>
        </div>
      </div>

      {percent !== null ? (
        <div
          className="progress-track"
          role="progressbar"
          aria-valuenow={done}
          aria-valuemin={0}
          aria-valuemax={total}
          aria-label="Accessibility rules processed"
        >
          <div className="progress-fill" style={{ width: `${percent}%` }} />
          <span className="progress-count">
            {done} of {total} rules
          </span>
        </div>
      ) : null}

      {lines.length ? (
        <div className="progress-log">
          {lines.map((line, index) => (
            <p key={index} className="progress-line">
              {line}
            </p>
          ))}
          <div ref={logEndRef} />
        </div>
      ) : null}

      <p className="progress-note">
        Chromium renders the page, axe-core finds the violations, the agent fixes
        them, then the whole page is audited again to check the work. Two to five
        minutes is normal.
      </p>
    </main>
  );
}
