"use client";

import { useState } from "react";
import { IS_REMOTE, startRun } from "@/lib/api";

interface Props {
  onStarted: (runId: string) => void;
  busy: boolean;
}

export default function RunStarter({ onStarted, busy }: Props) {
  const [url, setUrl] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  if (!IS_REMOTE) return null;

  async function submit() {
    const trimmed = url.trim();
    if (!/^https?:\/\//i.test(trimmed)) {
      setError("Enter a full URL, starting with https://");
      return;
    }
    setSubmitting(true);
    setError(null);
    try {
      const { runId } = await startRun([trimmed]);
      onStarted(runId);
    } catch (caught) {
      setError((caught as Error).message);
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="run-starter">
      <label className="visually-hidden" htmlFor="target-url">
        URL of the page to audit
      </label>
      <input
        id="target-url"
        type="url"
        className="url-input"
        placeholder="https://example.ac.in/"
        value={url}
        onChange={(event) => setUrl(event.target.value)}
        onKeyDown={(event) => {
          if (event.key === "Enter") submit();
        }}
        disabled={submitting || busy}
      />
      <button
        type="button"
        className="primary-button"
        onClick={submit}
        disabled={submitting || busy}
      >
        {submitting ? "Starting…" : "Audit and fix"}
      </button>
      {error ? (
        <p className="run-error" role="alert">
          {error}
        </p>
      ) : null}
    </div>
  );
}
