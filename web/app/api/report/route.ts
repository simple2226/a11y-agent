import { readFile } from "node:fs/promises";
import path from "node:path";
import { NextResponse } from "next/server";

export const dynamic = "force-dynamic";

/**
 * Reads the report the CLI wrote to `out/report.json`.
 * Set NEXT_PUBLIC_API_URL to read from the deployed API instead; this route is
 * the local development and offline-demo path.
 */
export async function GET() {
  const reportDirectory = process.env.REPORT_DIR ?? "../out";
  const reportPath = path.resolve(process.cwd(), reportDirectory, "report.json");

  try {
    const contents = await readFile(reportPath, "utf-8");
    return NextResponse.json(JSON.parse(contents));
  } catch {
    return NextResponse.json(
      {
        error: "no_report",
        message: `No report at ${reportPath}. Run the pipeline first:\n  python run.py --fixture evals/fixtures/broken-demo.html --out out/`,
      },
      { status: 404 },
    );
  }
}
