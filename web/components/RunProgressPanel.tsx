"use client";

import { useEffect, useRef, useState } from "react";
import {
  STALL_THRESHOLD_SECONDS,
  runFailure,
  secondsSinceHeartbeat,
  type RunSummary,
} from "@/lib/api";

interface Props {
  run: RunSummary | null;
  url: string | null;
  onRetry?: () => void;
}

function formatDuration(seconds: number): string {
  const minutes = Math.floor(seconds / 60);
  const remainder = seconds % 60;
  return minutes ? `${minutes}m ${remainder}s` : `${remainder}s`;
}

/**
 * What the agent is doing right now.
 *
 * A run takes minutes, and the only honest way to show that is to show the
 * work. The Lambda writes each graph node's log line to DynamoDB as it happens
 * and this renders them as they arrive.
 *
 * It also has to be honest when the work has stopped. The Lambda heartbeats
 * every ten seconds; if that stops, the invocation is gone and no amount of
 * further polling will bring a report back. Saying so beats a spinner that
 * turns for ever.
 */
export default function RunProgressPanel({ run, url, onRetry }: Props) {
  const logEndRef = useRef<HTMLDivElement | null>(null);
  const page = run?.pages?.[0];
  const progress = page?.progress ?? null;
  const lines = progress?.log ?? [];

  // Re-render on a timer as well as on new data, so the elapsed clock moves and
  // a stall is noticed even when polling returns an unchanged row.
  const [, setTick] = useState(0);
  useEffect(() => {
    const timer = setInterval(() => setTick((value) => value + 1), 1000);
    return () => clearInterval(timer);
  }, []);

  useEffect(() => {
    logEndRef.current?.scrollIntoView({ block: "end" });
  }, [lines.length]);

  const total = progress?.clustersTotal ?? 0;
  const done = progress?.clustersDone ?? 0;
  const percent = total > 0 ? Math.round((done / total) * 100) : null;

  const failure = runFailure(run);
  const sinceHeartbeat = secondsSinceHeartbeat(run) ?? 0;
  const stalled = !failure && sinceHeartbeat > STALL_THRESHOLD_SECONDS;
  const elapsed = progress?.elapsedSeconds ?? null;

  if (failure) {
    return (
      <main className="progress-panel progress-panel--failed" role="alert">
        <h2 className="progress-title">The run stopped</h2>
        <p className="progress-error">{failure}</p>
        {lines.length ? (
          <div className="progress-log">
            {lines.map((line, index) => (
              <p key={index} className="progress-line">
                {line}
              </p>
            ))}
          </div>
        ) : null}
        <p className="progress-note">
          Nothing was published. The page you pointed at is untouched — the agent
          only ever edits its own mirrored copy.
        </p>
        {onRetry ? (
          <button type="button" className="progress-retry" onClick={onRetry}>
            Start a new run
          </button>
        ) : null}
      </main>
    );
  }

  return (
    <main className="progress-panel">
      <div className="progress-head">
        <span className="spinner" aria-hidden="true" />
        <div>
          <h2 className="progress-title">
            Auditing {url ? <code>{url}</code> : "the page"}
            {elapsed !== null ? (
              <span className="progress-elapsed"> · {formatDuration(elapsed)}</span>
            ) : null}
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

      {stalled ? (
        <p className="progress-stall" role="status">
          No activity for {formatDuration(sinceHeartbeat)}. The run may have been
          cut short — give it another few seconds, then start a new one.
          {onRetry ? (
            <button type="button" className="progress-retry" onClick={onRetry}>
              Start over
            </button>
          ) : null}
        </p>
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