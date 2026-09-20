"use client";

import { useEffect, useRef } from "react";
import type { ReportWithArtifacts } from "@/lib/api";
import { frameSource } from "@/lib/api";

export type ViewMode = "split" | "original" | "patched";

interface Props {
  selectors: string[];
  scoreBefore: number;
  scoreAfter: number;
  viewMode: ViewMode;
  onViewModeChange: (mode: ViewMode) => void;
  report: ReportWithArtifacts | null;
}

const VARIANTS = [
  { variant: "original" as const, caption: "Before", colour: "#B3261E" },
  { variant: "patched" as const, caption: "After", colour: "#0F6E3F" },
];

const MODES: { mode: ViewMode; label: string }[] = [
  { mode: "original", label: "Before" },
  { mode: "split", label: "Side by side" },
  { mode: "patched", label: "After" },
];

export default function ComparisonFrames({
  selectors,
  scoreBefore,
  scoreAfter,
  viewMode,
  onViewModeChange,
  report,
}: Props) {
  const frameRefs = useRef<Record<string, HTMLIFrameElement | null>>({});

  useEffect(() => {
    function post() {
      for (const source of VARIANTS) {
        frameRefs.current[source.variant]?.contentWindow?.postMessage(
          { kind: "a11y-agent:highlight", selectors, colour: source.colour },
          "*",
        );
      }
    }
    function onMessage(event: MessageEvent) {
      if (event.data?.kind === "a11y-agent:ready") post();
    }
    window.addEventListener("message", onMessage);
    post();
    return () => window.removeEventListener("message", onMessage);
  }, [selectors, viewMode]);

  const shown =
    viewMode === "split" ? VARIANTS : VARIANTS.filter((entry) => entry.variant === viewMode);

  return (
    <div className="compare-region">
      <div className="view-switch" role="group" aria-label="Which version to show">
        {MODES.map((option) => (
          <button
            key={option.mode}
            type="button"
            className="view-button"
            aria-pressed={viewMode === option.mode}
            onClick={() => onViewModeChange(option.mode)}
          >
            {option.label}
          </button>
        ))}
      </div>

      <div className="frames" data-mode={viewMode}>
        {shown.map((source) => (
          <section
            key={source.variant}
            className="frame"
            data-variant={source.variant}
            aria-label={`${source.caption}: the page as the agent ${
              source.variant === "original" ? "found" : "left"
            } it`}
          >
            <div className="frame-caption">
              <span>{source.caption}</span>
              <span className="frame-score">
                {source.variant === "original" ? scoreBefore : scoreAfter}
                <span className="visually-hidden"> out of 100</span>
              </span>
            </div>
            <iframe
              ref={(element) => {
                frameRefs.current[source.variant] = element;
              }}
              src={frameSource(source.variant, report)}
              title={`${source.caption} rendering of the audited page`}
              // allow-popups lets a link the viewer clicks open the live page in
              // a new tab instead of navigating this frame to a site that
              // refuses to be framed. -to-escape-sandbox means that new tab is a
              // normal browser tab rather than a crippled sandboxed one.
              // Still withheld: allow-forms, allow-modals, allow-downloads,
              // allow-top-navigation -- the preview can never submit anything,
              // interrupt the page, or navigate the dashboard.
              sandbox="allow-same-origin allow-scripts allow-popups allow-popups-to-escape-sandbox"
            />
          </section>
        ))}
      </div>
    </div>
  );
}