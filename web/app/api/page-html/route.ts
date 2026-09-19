import { readFile } from "node:fs/promises";
import path from "node:path";

export const dynamic = "force-dynamic";

/**
 * Serves `original.html` or `patched.html` into the comparison iframes, with a
 * highlight bridge injected. The files on disk stay untouched -- the bridge is
 * added on the way out so the artifacts we ship are the real mirrored pages.
 */
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

export async function GET(request: Request) {
  const variant = new URL(request.url).searchParams.get("variant");
  if (variant !== "original" && variant !== "patched") {
    return new Response("variant must be 'original' or 'patched'", { status: 400 });
  }

  const reportDirectory = process.env.REPORT_DIR ?? "../out";
  const filePath = path.resolve(process.cwd(), reportDirectory, `${variant}.html`);

  let html: string;
  try {
    html = await readFile(filePath, "utf-8");
  } catch {
    return new Response(`Not found: ${filePath}`, { status: 404 });
  }

  const withBridge = html.includes("</body>")
    ? html.replace("</body>", `${HIGHLIGHT_BRIDGE}</body>`)
    : html + HIGHLIGHT_BRIDGE;

  return new Response(withBridge, {
    headers: {
      "Content-Type": "text/html; charset=utf-8",
      "X-Robots-Tag": "noindex, nofollow",
    },
  });
}
