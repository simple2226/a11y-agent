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

export interface PageSummary {
  pageId: string;
  url: string;
  title: string;
  status: string;
  scoreBefore: number | null;
  scoreAfter: number | null;
}

export interface RunSummary {
  runId: string;
  status: string;
  urls: string[];
  pages: PageSummary[];
  aggregate: {
    pagesDone: number;
    pagesTotal: number;
    scoreBefore: number | null;
    scoreAfter: number | null;
  };
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
