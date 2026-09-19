import { readFile } from "node:fs/promises";
import path from "node:path";

export const dynamic = "force-dynamic";

/**
 * Serves a mirrored page into a comparison iframe with the highlight bridge
 * injected.
 *
 * Two sources, one output:
 *   ?variant=original|patched     read out/ from disk (local CLI runs)
 *   ?src=<presigned S3 url>       fetch the deployed artifact and proxy it
 *
 * The proxy exists because the bridge has to be same-origin with the dashboard
 * to receive postMessage, and because the artifacts in S3 stay byte-identical
 * to what the pipeline produced -- we never write viewer code into them.
 */

// Only ever proxy our own artifact bucket. Without this the route is an open
// proxy that will fetch anything anyone puts in the query string.
function isAllowedSource(raw: string): boolean {
  let parsed: URL;
  try {
    parsed = new URL(raw);
  } catch {
    return false;
  }
  if (parsed.protocol !== "https:") return false;
  return (
    parsed.hostname.endsWith(".s3.amazonaws.com") ||
    /\.s3[.-][a-z0-9-]+\.amazonaws\.com$/.test(parsed.hostname)
  );
}
const HIGHLIGHT_BRIDGE = `
<script>
(function () {
  var STYLE_ID = "a11y-agent-highlight-style";
  var MARK_ATTRIBUTE = "data-a11y-agent-marked";

  function ensureStyle() {
    if (document.getElementById(STYLE_ID)) return;
    var style = document.createElement("style");
    style.id = STYLE_ID;
    style.textContent =
      "[" + MARK_ATTRIBUTE + "]{outline:3px solid var(--a11y-mark-colour,#B3261E)!important;" +
      "outline-offset:2px!important;scroll-margin:120px;}";
    document.head.appendChild(style);
  }

  function clearMarks() {
    document.querySelectorAll("[" + MARK_ATTRIBUTE + "]").forEach(function (node) {
      node.removeAttribute(MARK_ATTRIBUTE);
      node.style.removeProperty("--a11y-mark-colour");
    });
  }

  window.addEventListener("message", function (event) {
    var payload = event.data;
    if (!payload || payload.kind !== "a11y-agent:highlight") return;
    ensureStyle();
    clearMarks();
    if (!payload.selectors || !payload.selectors.length) return;

    var first = null;
    payload.selectors.forEach(function (selector) {
      var matches;
      try { matches = document.querySelectorAll(selector); } catch (error) { return; }
      matches.forEach(function (node) {
        node.setAttribute(MARK_ATTRIBUTE, "");
        node.style.setProperty("--a11y-mark-colour", payload.colour || "#B3261E");
        if (!first) first = node;
      });
    });
    if (first && payload.scroll !== false) {
      first.scrollIntoView({ block: "center", behavior: "smooth" });
    }
  });

  parent.postMessage({ kind: "a11y-agent:ready" }, "*");
})();
</script>
`;

function withBridge(html: string): string {
  return html.includes("</body>")
    ? html.replace("</body>", `${HIGHLIGHT_BRIDGE}</body>`)
    : html + HIGHLIGHT_BRIDGE;
}

const HTML_HEADERS = {
  "Content-Type": "text/html; charset=utf-8",
  // A mirrored copy of someone else's page must never be indexed.
  "X-Robots-Tag": "noindex, nofollow",
  "Cache-Control": "no-store",
};

export async function GET(request: Request) {
  const params = new URL(request.url).searchParams;
  const source = params.get("src");

  if (source) {
    if (!isAllowedSource(source)) {
      return new Response("src must be an https S3 artifact URL", { status: 400 });
    }
    try {
      const upstream = await fetch(source, { cache: "no-store" });
      if (!upstream.ok) {
        // A presigned URL expires after an hour; say so rather than showing a
        // blank frame and an S3 XML error.
        return new Response(
          `Could not load the artifact (${upstream.status}). ` +
            `The link may have expired -- reload the page to get a fresh one.`,
          { status: 502 },
        );
      }
      return new Response(withBridge(await upstream.text()), { headers: HTML_HEADERS });
    } catch {
      return new Response("Could not reach the artifact store.", { status: 502 });
    }
  }

  const variant = params.get("variant");
  if (variant !== "original" && variant !== "patched") {
    return new Response("variant must be 'original' or 'patched'", { status: 400 });
  }

  const reportDirectory = process.env.REPORT_DIR ?? "../out";
  const filePath = path.resolve(process.cwd(), reportDirectory, `${variant}.html`);

  try {
    const html = await readFile(filePath, "utf-8");
    return new Response(withBridge(html), { headers: HTML_HEADERS });
  } catch {
    return new Response(`Not found: ${filePath}`, { status: 404 });
  }
}
