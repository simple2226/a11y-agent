"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import ChangeDetail from "@/components/ChangeDetail";
import ComparisonFrames, { type ViewMode } from "@/components/ComparisonFrames";
import RunProgressPanel from "@/components/RunProgressPanel";
import RunFacts from "@/components/RunFacts";
import RunStarter from "@/components/RunStarter";
import RuleList from "@/components/RuleList";
import VerdictBar from "@/components/VerdictBar";
import {
  IS_REMOTE,
  fetchReport,
  fetchRun,
  runFailure,
  type ReportWithArtifacts,
  type RunSummary,
} from "@/lib/api";
import { buildRuleRows } from "@/lib/rules";

const POLL_INTERVAL_MS = 2500;

export default function Dashboard() {
  const [runId, setRunIdState] = useState<string | null>(null);

  const setRunId = useCallback((id: string | null) => {
    setRunIdState(id);
    if (typeof window === "undefined") return;
    const url = new URL(window.location.href);
    if (id) {
      url.searchParams.set("run", id);
    } else {
      url.searchParams.delete("run");
    }
    window.history.replaceState(null, "", url.toString());
  }, []);

  // Pick a run back up after a reload, or from a shared link.
  useEffect(() => {
    if (!IS_REMOTE) return;
    const existing = new URLSearchParams(window.location.search).get("run");
    if (existing) setRunIdState(existing);
  }, []);
  const [run, setRun] = useState<RunSummary | null>(null);
  const [report, setReport] = useState<ReportWithArtifacts | null>(null);
  const [selectedPageId, setSelectedPageId] = useState<string | null>(null);
  const [selectedRuleId, setSelectedRuleId] = useState<string | null>(null);
  const [viewMode, setViewMode] = useState<ViewMode>("split");
  const [loadError, setLoadError] = useState<string | null>(null);
  const pollTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  // Local mode has no run concept -- there is exactly one report on disk.
  useEffect(() => {
    if (IS_REMOTE) return;
    fetchReport("", "")
      .then(setReport)
      .catch((error: Error) => setLoadError(error.message));
  }, []);

  const poll = useCallback(async (id: string) => {
    try {
      const summary = await fetchRun(id);
      setRun(summary);

      const finished = summary.pages.filter((page) => page.status === "done");
      if (finished.length) {
        const target =
          finished.find((page) => page.pageId === selectedPageId) ?? finished[0];
        setSelectedPageId(target.pageId);
        setReport(await fetchReport(id, target.pageId));
      }

      // A failed run never becomes a finished one. Polling it for ever is what
      // makes a broken run look like a slow one.
      const stillRunning =
        summary.aggregate.pagesDone < summary.aggregate.pagesTotal &&
        !runFailure(summary);
      if (stillRunning) {
        pollTimer.current = setTimeout(() => poll(id), POLL_INTERVAL_MS);
      }
    } catch (error) {
      setLoadError((error as Error).message);
    }
  }, [selectedPageId]);

  useEffect(() => {
    if (!runId) return;
    poll(runId);
    return () => {
      if (pollTimer.current) clearTimeout(pollTimer.current);
    };
    // poll is intentionally excluded: it changes with selectedPageId and would
    // restart the timer on every page switch.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [runId]);

  const rows = useMemo(() => (report ? buildRuleRows(report) : []), [report]);

  useEffect(() => {
    if (rows.length && !rows.some((row) => row.ruleId === selectedRuleId)) {
      setSelectedRuleId(rows[0].ruleId);
    }
  }, [rows, selectedRuleId]);

  const selectedRow = rows.find((row) => row.ruleId === selectedRuleId) ?? null;
  const waiting = Boolean(runId) && !report;

  const resetRun = useCallback(() => {
    if (pollTimer.current) clearTimeout(pollTimer.current);
    setRun(null);
    setLoadError(null);
    setRunId(null);
  }, [setRunId]);

  if (!report && !loadError && !IS_REMOTE) {
    return (
      <main className="empty">
        <p>Loading the run…</p>
      </main>
    );
  }

  if (!report && !IS_REMOTE) {
    return (
      <main className="empty">
        <h1>No run to show yet</h1>
        <p>{loadError}</p>
        <pre>python run.py https://example.ac.in/ --out out/</pre>
      </main>
    );
  }

  return (
    <div className="shell">
      <header className="masthead">
        <div className="masthead-identity">
          <h1 title={report?.page_title || undefined}>
            {report?.page_title || "a11y-agent"}
          </h1>
          {report ? (
            <span className="source" title={report.page_url}>
              {report.page_url}
            </span>
          ) : (
            <span className="source">accessibility remediation agent</span>
          )}
        </div>

        <RunStarter onStarted={setRunId} busy={waiting} />
      </header>

      {report && !waiting ? <VerdictBar report={report} rows={rows} /> : null}

      {waiting ? (
        <RunProgressPanel
          run={run}
          url={run?.urls?.[0] ?? null}
          onRetry={resetRun}
        />
      ) : null}

      {report && !waiting ? (
        <div className="workspace">
          <div className="sidebar">
            <RuleList rows={rows} selectedRuleId={selectedRuleId} onSelect={setSelectedRuleId} />
            <RunFacts report={report} />
          </div>
          <div className="compare">
            <ComparisonFrames
              selectors={selectedRow?.selectors ?? []}
              scoreBefore={report.score_before}
              scoreAfter={report.score_after}
              viewMode={viewMode}
              onViewModeChange={setViewMode}
              report={report}
            />
            <ChangeDetail row={selectedRow} />
          </div>
        </div>
      ) : null}

      {IS_REMOTE && !report && !waiting ? (
        <main className="landing">
          <h2>Paste a URL and watch it get fixed.</h2>
          <p>
            The agent renders the page in headless Chromium, runs axe-core
            against it, and gets a score. It then works one WCAG rule at a time:
            propose edits, apply them to a private copy, re-audit, and keep the
            change only if the score actually went up. You see both versions side
            by side with every edit it made.
          </p>
          <ol className="landing-steps">
            <li>Chromium renders the page and axe-core scores it</li>
            <li>The agent fixes one rule, then re-audits to check itself</li>
            <li>Anything that made the page worse is rolled back</li>
          </ol>
          <p className="landing-note">
            Two to five minutes for a typical page. The site you point it at is
            never modified — every edit lands on a copy.
          </p>
        </main>
      ) : null}

      {loadError && report ? (
        <p className="run-error" role="alert">
          {loadError}
        </p>
      ) : null}
    </div>
  );
}