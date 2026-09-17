# a11y-agent

An agent that finds WCAG 2.1 AA accessibility violations on a public web page,
generates fixes, applies them, **re-audits its own work**, and retries what it
broke — then reports honestly on what it could not fix.

Built for First Commit (Bharat Builds Tour), 17–20 September 2026.

---

## Why this is verifiable

Most AI demos ask you to trust the output. This one does not. The agent's work
is graded by axe-core, an independent open-source accessibility engine that we
did not write and cannot influence. The before and after scores in the demo come
from the same tool a judge can run against this repo.

The agent is also allowed to fail, and says so. `deferred` and `unfixed_rules`
in every report are first-class output.

## The loop

```
mirror ──▶ audit_original ──▶ cluster ──┐
                                        ▼
                        ┌──────▶ generate_edits (Bedrock, forced tool call)
                        │               │
                        │               ▼
                        │         apply_edits (lxml, deterministic)
                        │               │
                        │               ▼
                        │           verify (axe re-run)
                        │               │
                        └── repair ◀────┤ rule unresolved, or regression
                          (max 2)       │
                                        ▼ resolved
                                   next_cluster ──▶ finalise
```

The feedback signal is real: rejected-edit reasons and the newly introduced
violations from the re-audit go straight back into the repair prompt.

## Three design decisions that matter

**1. We mirror the page instead of patching the live site.**
You cannot modify a site you do not own. `audit/mirror.py` fetches the page,
injects `<base href>`, strips CSP meta tags that would block cross-origin
assets, and rewrites `srcset` and inline `url()`. The mirrored copy renders
identically, which is the only reason before/after scores are comparable.

**2. The model never writes HTML.**
It emits a JSON array of structured edit operations — `set_attribute`,
`wrap_element`, `add_css_rule` and four others — each keyed by a CSS selector.
`agent/applier.py` applies them with lxml. Anything targeting `html`, `body`,
`*` or `script`, anything matching zero nodes or more than 40, anything setting
an inline event handler, and any unparseable selector is rejected before it
touches the DOM. A hallucinated selector cannot break a layout; it produces a
rejection reason that gets fed back to the model.

**3. The model is told not to guess.**
It cannot see the images. Generating alt text from a filename is worse than no
alt text at all, because a screen reader user cannot detect that it is wrong.
The tool schema has a `deferred` array, and the prompt for `image-alt` says
explicitly that deferring most nodes is the expected outcome. Only images that
the *markup* identifies as decorative — `class="icon"`, `class="spacer"`, 1×1
sizing, an image inside an already-labelled link — get `alt=""` +
`role="presentation"`.

## The score

`audit/scorer.py`, published formula, no black box:

```
penalty = Σ  impact_weight(rule) × min(affected_nodes, 10)
          critical=10  serious=7  moderate=3  minor=1
score   = max(0, 100 − min(100, penalty))
```

Node count is capped per rule so one rule failing on 400 nodes cannot zero the
page and hide improvements elsewhere. This is **not** the Lighthouse score — we
compute our own so the number is reproducible from this repo alone.

## Running it

```bash
./scripts/setup.sh

# audit only, no model calls, no AWS, no network
python local_run.py --fixture evals/fixtures/broken-demo.html --no-agent

# vet a real target before building a demo around it
python scripts/check_target.py https://example.ac.in/

# full agent run
python local_run.py https://example.ac.in/ --out out/

# the eval table
python evals/run_evals.py
```

Outputs land in `out/`: `original.html`, `patched.html`, `report.json`, `run.log`.

## AWS

| Service | Role |
|---|---|
| **Bedrock** (Claude Sonnet, Converse + forced `toolChoice`) | generates structured edits; forced tool use means we never parse JSON out of prose |
| **Lambda** (container image, 3008 MB) | Playwright + Chromium + axe-core; agent loop |
| **Step Functions** | `Map` over seed URLs, retry/catch per page |
| **S3** | original/patched HTML, screenshots, axe JSON — **private**, 7-day lifecycle expiry |
| **DynamoDB** | run ledger: `pk=RUN#<id>`, `sk=PAGE#<id>` / `VIOLATION#<id>` |
| **API Gateway HTTP API + Lambda** | FastAPI via Mangum |
| **Amplify Hosting** | Next.js dashboard |

Deploy: `cd infra && sam build && sam deploy --guided`

Container gotcha: the Lambda AL2023 base image uses `dnf`, so
`playwright install --with-deps` (which shells out to `apt`) fails. The
Dockerfile installs the Chromium shared libraries explicitly.

## Ethics and legal position

- Fetches only publicly accessible pages. Never authenticated content, never
  anything behind a paywall.
- Checks and obeys `robots.txt`, rate limits to 1 request/second, depth 1,
  hard cap of 5 pages.
- Identifies itself honestly in the User-Agent.
- Stores no personal data.
- **Patched copies are never published.** The S3 bucket blocks all public
  access; the dashboard reads through short-lived presigned URLs, and a
  lifecycle rule deletes everything after 7 days. This is an analysis tool, not
  a republisher.
- Demo targets are anonymised. We are not naming or shaming anyone's web team.

## Known limitations

- **Colour contrast fixes are computed from declared CSS, not rendered pixels.**
  A rule that wins the cascade elsewhere can defeat our patch. `verify` catches
  it and the rule ends up in `unfixed_rules`.
- **Alt text is mostly deferred, by design.** See decision 3. A future version
  sends the image to a multimodal model; we did not build that in 4 days.
- **No JavaScript-rendered SPA support beyond initial hydration.** We wait for
  `networkidle` plus 1.5 s and audit whatever exists then.
- **The mirror is not a perfect archiver.** Sites with aggressive CSP headers
  (not just meta tags), signed asset URLs, or origin hotlink protection will
  render degraded, which skews the score. `scripts/check_target.py` exists to
  catch this before you build a demo on a bad target.
- Fixes are advisory. Nothing here replaces a manual audit with a real screen
  reader and a real user.

## What we learned

_(fill this in Sunday morning — it is a scored criterion and the failures are
the interesting part)_
