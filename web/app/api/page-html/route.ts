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

  // ------------------------------------------------------------------
  // Navigation guard.
  //
  // The mirror injects <base href="https://the-real-site/"> so relative URLs
  // for images and CSS resolve. That also means every link in the snapshot
  // points at the live site, and clicking one makes the iframe navigate there.
  // Most sites send X-Frame-Options, so the browser refuses and the preview is
  // replaced by a "refused to connect" error page -- the comparison is gone
  // until you re-run.
  //
  // This is a snapshot of one page, not a browsable site: there is nothing to
  // navigate to. So clicks are swallowed and the reason is shown, rather than
  // leaving a dead end that looks like a bug.
  var NOTICE_ID = "a11y-agent-nav-notice";

  function showNotice(message) {
    var notice = document.getElementById(NOTICE_ID);
    if (!notice) {
      notice = document.createElement("div");
      notice.id = NOTICE_ID;
      notice.setAttribute("role", "status");
      notice.style.cssText =
        "position:fixed;left:50%;bottom:16px;transform:translateX(-50%);z-index:2147483647;" +
        "max-width:min(34rem,90vw);padding:10px 14px;border-radius:6px;" +
        "background:#1b2430;color:#fff;font:14px/1.45 system-ui,sans-serif;" +
        "box-shadow:0 4px 16px rgba(0,0,0,.28);text-align:center;";
      document.body.appendChild(notice);
    }
    notice.textContent = message;
    notice.style.display = "block";
    clearTimeout(showNotice.timer);
    showNotice.timer = setTimeout(function () { notice.style.display = "none"; }, 2600);
  }

  // Point every outbound link at a new tab rather than at this frame.
  //
  // Letting a link navigate the frame is what produced "refused to connect":
  // <base href> resolves it to the live site, which sends X-Frame-Options, and
  // the comparison is replaced by a browser error page. Blocking the click
  // outright is safe but a dead end. Opening the real page in a new tab is the
  // honest version of what the click means -- the preview is a saved copy of
  // one page, and the thing being linked to only exists on the live site.
  function retarget(anchor) {
    var href = anchor.getAttribute("href") || "";
    if (!href || href.charAt(0) === "#") return;
    var resolved = anchor.href || "";
    if (!/^https?:/i.test(resolved)) return;
    anchor.target = "_blank";
    anchor.rel = "noopener noreferrer";
  }

  function retargetAll() {
    var links = document.querySelectorAll("a[href]");
    for (var i = 0; i < links.length; i++) retarget(links[i]);
  }
  retargetAll();
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", retargetAll);
  }

  var toldThem = false;

  // Capture phase, so a site's own handlers cannot get there first.
  document.addEventListener("click", function (event) {
    var anchor = event.target && event.target.closest ? event.target.closest("a[href]") : null;
    if (!anchor) return;

    var href = anchor.getAttribute("href") || "";

    // A fragment link is NOT safe to let through here. <base href> makes "#main"
    // resolve against the BASE url rather than the document, so a skip link
    // navigates to https://the-real-site/#main and the preview is gone. Skip
    // links are exactly what an accessibility reviewer wants to try, so they are
    // honoured manually instead of being disabled.
    if (href.charAt(0) === "#") {
      event.preventDefault();
      event.stopPropagation();
      var id = href.slice(1);
      if (!id) {
        window.scrollTo({ top: 0, behavior: "smooth" });
        return;
      }
      var target = null;
      try {
        target = document.getElementById(id) ||
          document.querySelector('[name="' + CSS.escape(id) + '"]');
      } catch (error) { /* exotic id, fall through */ }
      if (target) {
        target.scrollIntoView({ block: "start", behavior: "smooth" });
        // Move focus too: a skip link that scrolls but does not focus is the
        // bug this whole project exists to find.
        if (!target.hasAttribute("tabindex")) target.setAttribute("tabindex", "-1");
        target.focus({ preventScroll: true });
      }
      return;
    }

    // Retarget here too, not just on load: this also covers links a script
    // added after the initial pass. The default action is resolved after the
    // event is dispatched, so setting target now is enough -- no preventDefault,
    // the click proceeds and lands in a new tab.
    retarget(anchor);

    if (anchor.target !== "_blank") {
      // Not an http(s) link -- javascript:, mailto: on a weird scheme, and so
      // on. Nothing sensible to open, so swallow it rather than let it navigate
      // the frame.
      event.preventDefault();
      event.stopPropagation();
      showNotice("That link does not go anywhere in this preview.");
      return;
    }

    if (!toldThem) {
      toldThem = true;
      showNotice("Opening the live page in a new tab. This preview is a saved copy of one page.");
    }
  }, true);

  document.addEventListener("submit", function (event) {
    event.preventDefault();
    event.stopPropagation();
    showNotice("Forms are disabled in the preview -- nothing is ever sent to the real site.");
  }, true);

  // Inline scripts survive the mirror, and some of them call window.open on
  // load. The sandbox blocks popups anyway, but silently -- this at least
  // explains itself.
  try {
    window.open = function () {
      showNotice("Pop-ups are disabled in the preview.");
      return null;
    };
  } catch (error) { /* frozen in some documents; not worth failing over */ }

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