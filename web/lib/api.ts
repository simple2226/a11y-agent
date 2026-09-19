import type { Report } from "./types";

/**
 * One data layer, two sources.
 *
 * NEXT_PUBLIC_API_URL set  -> the deployed API (what Amplify serves)
 * NEXT_PUBLIC_API_URL unset -> the local out/report.json the CLI writes
 *
 * Both return the same Report shape, because agent/handler.py writes the same
 * JSON to S3 that local_run.py writes to disk.
 */
export const API_BASE = process.env.NEXT_PUBLIC_API_URL?.replace(/\/$/, "") ?? "";
export const IS_REMOTE = API_BASE.length > 0;

export interface RunProgress {
  phase: string;
  clustersDone: number;
  clustersTotal: number;
  log: string[];
  /** Seconds since the page started, as the Lambda measured it. */
  elapsedSeconds?: number;
  /** Unix seconds. The Lambda heartbeats this every 10s, so a value that stops
   *  advancing means the invocation died rather than that a node is slow. */
  updatedAt: number;
}

export interface PageSummary {
  pageId: string;
  url: string;
  title: string;
  status: string;
  scoreBefore: number | null;
  scoreAfter: number | null;
  /** Written by the agent Lambda after every graph node while a run is live. */
  progress?: RunProgress | null;
  /** Set only when status === "failed". */
  error?: string | null;
}

export interface RunSummary {
  runId: string;
  status: string;
  /** Set when the run failed outside the agent — a killed Lambda, say. */
  error?: string | null;
  urls: string[];
  pages: PageSummary[];
  aggregate: {
    pagesDone: number;
    pagesFailed?: number;
    pagesTotal: number;
    scoreBefore: number | null;
    scoreAfter: number | null;
  };
}

/** Seconds with no heartbeat before the UI stops claiming the run is healthy.
 *  The Lambda beats every 10s; 75s is six missed beats plus slack for a slow
 *  DynamoDB write, so this does not fire on a merely slow run. */
export const STALL_THRESHOLD_SECONDS = 75;

export function runFailure(run: RunSummary | null): string | null {
  if (!run) return null;

  const failedPage = run.pages.find((page) => page.status === "failed");
  if (failedPage) return failedPage.error || "The agent stopped with an error.";
  if (run.status === "failed") {
    return run.error || "The run stopped with an error.";
  }

  return null;
}

/** Seconds since the last heartbeat, or null when there is nothing to judge. */
export function secondsSinceHeartbeat(run: RunSummary | null): number | null {
  const updatedAt = run?.pages?.[0]?.progress?.updatedAt;
  if (!updatedAt) return null;
  return Math.max(0, Math.round(Date.now() / 1000 - updatedAt));
}

export interface ReportWithArtifacts extends Report {
  artifacts?: { original?: string; patched?: string };
}

async function getJson<T>(url: string): Promise<T> {
  const response = await fetch(url, { cache: "no-store" });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(body.message ?? body.detail ?? `Request failed (${response.status})`);
  }
  return response.json() as Promise<T>;
}

export async function startRun(urls: string[]): Promise<{ runId: string }> {
  if (!IS_REMOTE) throw new Error("Starting runs requires NEXT_PUBLIC_API_URL.");
  const response = await fetch(`${API_BASE}/runs`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ urls }),
  });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(body.detail ?? `Could not start the run (${response.status})`);
  }
  return response.json();
}

export function fetchRun(runId: string): Promise<RunSummary> {
  return getJson<RunSummary>(`${API_BASE}/runs/${runId}`);
}

export function fetchReport(runId: string, pageId: string): Promise<ReportWithArtifacts> {
  if (!IS_REMOTE) return getJson<ReportWithArtifacts>("/api/report");
  return getJson<ReportWithArtifacts>(`${API_BASE}/runs/${runId}/pages/${pageId}/report`);
}

/**
 * Where a comparison iframe points.
 *
 * Both modes go through /api/page-html, which injects the highlight bridge. A
 * presigned S3 URL is proxied rather than used directly: the bridge has to be
 * same-origin with the dashboard to receive postMessage, and the objects in S3
 * stay byte-identical to what the pipeline wrote.
 */
export function frameSource(
  variant: "original" | "patched",
  report: ReportWithArtifacts | null,
): string {
  if (!IS_REMOTE) return `/api/page-html?variant=${variant}`;

  const artifact = report?.artifacts?.[variant];
  if (!artifact) return "about:blank";
  return `/api/page-html?src=${encodeURIComponent(artifact)}`;
}