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
                        ┌──────▶ generate_edits (forced tool call)
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
| **Bedrock** (Converse + forced `toolChoice`) | generates structured edits; forced tool use means we never parse JSON out of prose. Swappable — see *Model providers* below |
| **Lambda** (container image, 3008 MB) | Playwright + Chromium + axe-core; agent loop |
| **Step Functions** | `Map` over seed URLs, retry/catch per page |
| **S3** | original/patched HTML, screenshots, axe JSON — **private**, 7-day lifecycle expiry |
| **DynamoDB** | run ledger: `pk=RUN#<id>`, `sk=PAGE#<id>` / `VIOLATION#<id>` |
| **API Gateway HTTP API + Lambda** | FastAPI via Mangum |
| **Amplify Hosting** | Next.js dashboard |

Deploy: `cd infra && ./deploy.ps1` (Windows) or `./deploy.sh`. Model
configuration is passed as CloudFormation parameters, so it survives redeploys:

```bash
export MODEL_PROVIDER=gemini
export GEMINI_API_KEY=AIza...
./infra/deploy.sh
```

### Model providers

Bedrock is the default. The account we built on was blocked from Bedrock
inference at the account level, so the backend is a runtime choice:

| `MODEL_PROVIDER` | Credentials | Notes |
|---|---|---|
| `bedrock` | IAM | Default. `scripts/bedrock_enable.py --check` diagnoses access |
| `gemini` | `GEMINI_API_KEY` | Free tier has a daily cap *and* sheds load under demand |
| `anthropic` | `ANTHROPIC_API_KEY` | |
| `openai` | `OPENAI_API_KEY` | |
| `groq` | `GROQ_API_KEY` | Free, fast, OpenAI-compatible |
| `openrouter` | `OPENROUTER_API_KEY` | |
| `ollama` | none | Local, `OPENAI_BASE_URL` overridable |
| `mock` | none | Deterministic rules, no network |

They all return the same `ModelEdits`, so the agent loop never changes. Every
run prints the active provider and model as its first line of output.

**`MODEL_PROVIDER` takes a chain**, tried in order:

```
MODEL_PROVIDER=gemini,groq,mock
```

This exists because of a specific failure. A run died after clustering with
`gemini failed after 5 attempts -- HTTP 503: This model is currently
experiencing high demand`. That is not a rate limit and not a bug on our side;
it is Google's free tier out of capacity, and no retry policy fixes it. The
chain shares one time budget — three providers do not take three times as long —
and each pass records which backend answered:

```
fix color-contrast [groq]: model proposed 6 edit(s), 6 applied, 0 rejected
```

That label is not decoration. With a fallback chain, "which model wrote these
edits" stops being answerable from configuration, and a demo that quietly
dropped to the deterministic rules while presenting itself as an LLM agent would
be misrepresenting itself. **No number in our demo or eval table comes from mock
mode** — the log is what proves it.

The chain distinguishes two kinds of failure, because they deserve opposite
treatment:

| | example | behaviour |
|---|---|---|
| transient | 503 high demand, 429, timeout | retried with backoff, then the next provider |
| fatal | 402 credits depleted, 401 bad key, 404 retired model | provider removed from the chain for the rest of the process |

The second row came from a run that returned `gemini 402: Your prepayment
credits are depleted`. That is a billing wall, and asking again on the next
cluster buys nothing but a round trip — eight rules would have meant being told
the same thing eight times. A cold start clears the list, which is the right
granularity for "someone topped the account up".

`mock` exists so the pipeline can be developed and the frontend built with no
credentials at all. It covers `image-alt`, `label`, `link-name`, `button-name`,
`color-contrast` and the simple attribute rules; anything else it defers
honestly rather than guessing.

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
- `evals/fixtures/` holds cached copies of public pages, retained only so the
  evaluation is reproducible. They are not republished, and we will delete any
  of them on request from the site owner.

## Known limitations

- **Colour contrast fixes are computed from declared CSS, not rendered pixels.**
  A rule that wins the cascade elsewhere can defeat our patch. `verify` catches
  it and the rule ends up in `unfixed_rules`.
- **Alt text is mostly deferred, by design.** See decision 3. A future version
  sends the image to a multimodal model; we did not build that in 4 days.
- **The audit blocks the page's own JavaScript.** axe reads the DOM and computed
  CSS, and those scripts are what prevented the page from ever finishing
  parsing. The cost is that client-side-rendered content and lazy-loaded images
  are not seen — on a Drupal site with `blazy`, every `<img>` is present in the
  DOM (so `image-alt` is unaffected) but none of them load. Every audit records
  what it blocked in `AuditResult.notes`, so this is visible rather than silent.
- **The mirror is not a perfect archiver.** Sites with aggressive CSP headers
  (not just meta tags), signed asset URLs, or origin hotlink protection will
  render degraded, which skews the score. `scripts/check_target.py` exists to
  catch this before you build a demo on a bad target.
- Fixes are advisory. Nothing here replaces a manual audit with a real screen
  reader and a real user.

## What we learned

Four days, and almost none of it went where we expected. The interesting
failures, in the order they bit us.

### The model provider was the wrong thing to depend on

We designed around Amazon Bedrock. On Thursday, every Bedrock call from this
account returned `ValidationException: Operation not allowed` — for Anthropic,
Amazon Nova **and** Meta models alike. That combination rules out the
per-provider gates and points at an account-level block, which no model id,
region or usage form can clear. We filed a support case; it did not resolve
inside the event window.

So we put a provider interface behind one function and made the backend a
runtime choice: `MODEL_PROVIDER=bedrock|gemini|anthropic|openai|mock`, one
`ModelEdits` shape out of all of them, and `agent/graph.py` unchanged. It took
about forty minutes and it is the single most valuable thing in the repo.
`scripts/bedrock_enable.py --check` was written in the same sitting: it reports
the four availability gates separately (`regionAvailability`,
`authorizationStatus`, `entitlementAvailability`, `agreementAvailability`)
instead of the one opaque string, so the next person does not spend an evening
guessing which one is closed.

**The lesson we did not expect:** our first diagnostic *lied*. It mapped every
"Operation not allowed" to the Anthropic usage form, so it confidently blamed
Anthropic for a Nova failure and sent us down the wrong path for an hour. A
diagnostic that can only produce one answer is not a diagnostic.

### The mirror was the whole project, and it was broken for two days

To compare before and after honestly you must audit the *same page* twice, so
we mirror the live page and patch the copy. Getting that copy to render took
four distinct fixes, and until the last one every score we produced was
fiction — a real university homepage "scored" 86/100 because axe was auditing a
nearly-empty DOM.

1. `set_content()` never fires `DOMContentLoaded` on a page with synchronous
   external scripts, because those scripts block parsing. Write the HTML to a
   file and `goto("file://…")` instead.
2. Those same scripts are useless to axe, which reads the DOM and computed CSS.
   Blocking `script`, `media` and `font` requests turned a 20-second timeout
   into a 3-second audit. (`font` is in that list because Playwright's
   screenshot waits on `document.fonts.ready`, and a webfont the origin never
   serves hangs it for the full timeout.)
3. Route interception with a `**/*` pattern swallows the `file://` navigation
   itself. Register handlers for `http://**` and `https://**` only.
4. **The one that mattered.** A `file://` document is an opaque origin, so
   Chromium refuses every `https://origin/…` stylesheet as cross-origin and
   applies none of them. `<base href>` cannot fix this. We fetch the
   stylesheets ourselves and inline them, resolving one level of `@import` and
   absolutising `url()` against the right base. The site turned out to have 47
   stylesheets, not the 21 the browser reported — the rest arrive via
   `@import` — so our first cap of 15 silently left the theme CSS unapplied.

The diagnostic we should have written first is `scripts/inspect_mirror.py`. It
reports what the browser actually received — stylesheet rule count, images
loaded, computed body font — and the honest signal turned out to be
`body font: "Times New Roman"`, not the rule count we were checking. A high
score on a real site almost always means the audit saw nothing, not that the
site is good.

### Verification changes what the agent is allowed to do

The first end-to-end run on a real site scored **65 → 58**. The agent fixed the
`list` rule by restructuring a `<ul>`, which orphaned its `<li>` children and
introduced `listitem`. Verification caught it; repair could not clear it; the
broken edits shipped anyway.

That is the failure mode that makes "AI fixes your site" untrustworthy, so we
made it structurally impossible. Every cluster snapshots the document and the
edit list before its first attempt. If it introduces a new rule, or simply
leaves the score lower than it found it, every edit it made is rolled back and
the rule is reported `reverted`. **A run cannot lower the score.** Not "usually
does not" — cannot.

We would rather report `65 → 65, 1 reverted` than `65 → 58` with a fix we are
proud of.

### Restraint has to be designed for, then designed against

The agent cannot see the images, so generating alt text from a filename is
worse than leaving it missing: a wrong description is undetectable to a screen
reader user, while a missing one is at least obvious. The tool schema therefore
has a first-class `deferred` array, and `image-alt` is only auto-fixed where the
markup positively says the image is decorative (`class="icon"`, `spacer.gif`,
1×1 sizing, an image inside an already-labelled link).

Then the opposite problem appeared. Told firmly enough not to guess, Gemini
started deferring `label` violations on inputs that carried
`placeholder="Search"` and `name="q"` — the answer was sitting in the markup. We
had to add per-rule guidance saying that deferring something derivable is
exactly as wrong as inventing something that is not, with an explicit lookup
order: label → placeholder → value → visible text → name attribute.

Calibrating an agent's willingness to act is not one instruction. It is two
opposing ones, per rule.

### Small engineering details that cost real hours

- **Specificity.** `add_css_rule` appended a `<style>` to `<head>`, which loses
  every contest against a site's own 7,899 rules. Four contrast edits applied
  cleanly and changed nothing, three times over. The patch block now goes last
  in `<body>` with `!important` on every declaration.
- **Retry belongs at the smallest scope that can recover.** Only our Bedrock
  client had backoff. When Gemini returned a single transient 503, the Lambda
  died, Step Functions retried the whole invocation, and the mirror plus every
  audit ran again from scratch. Transient failures now retry in place, and the
  Step Functions retry was narrowed to genuine Lambda infrastructure errors.
- **Scoring can hide the result.** Our first formula let one rule failing on 11
  nodes exhaust the whole 100-point penalty, so every bad page scored 0 and
  `0 → 9` read like the tool had failed to measure. Capping each rule's
  contribution at three nodes turned the same run into `12 → 49`.
- **Flags beat environment variables under time pressure.** `A11Y_MOCK_MODEL`
  only existed as an env var, and a forgotten `$env:` in a fresh terminal sent a
  run at a blocked Bedrock endpoint. `--provider` now overrides it, resolved per
  call rather than at import, and every run prints the model it is about to use
  as its first line of output.
- **Playwright ships no Amazon Linux 2023 build.** It falls back to the Ubuntu
  20.04 binary, which works once the shared libraries are installed with `dnf`
  rather than `apt`. `--single-process` and `--no-zygote` are required in Lambda
  and crash the renderer on a desktop, so the launch flags are chosen from
  `AWS_LAMBDA_FUNCTION_NAME` at runtime. `scripts/smoke_image.py` verifies all
  of this inside the built image, before a 2 GB push to ECR.

### A hang is a worse failure than an error

A run stopped dead after the clustering step and stayed there. The dashboard
kept polling, the spinner kept turning, and the last log line still read
`cluster: 3 cluster(s) queued` — which is exactly what a healthy run looks like
one second in. Nothing anywhere said what had happened.

Three separate faults lined up:

- **The retry policy could outlive the Lambda.** Five attempts at a 180-second
  timeout with 30-second backoffs is seventeen minutes inside a fifteen-minute
  function. The Lambda was killed mid-call, so the `finally` never ran and no
  result was ever written.
- **The handler had no `except`.** A raised exception left the page row on
  `status: "running"` for ever. There was no state in the system that meant
  *failed*.
- **Progress was written per node, not per second.** The longest node is the
  model call, and during it `updatedAt` froze — so nothing could tell a slow run
  from a dead one, in the UI or by eye.

The fix is three nested budgets — 75s per HTTP attempt, 240s per model call
including retries, 600s for the fixing loop inside a 900s Lambda — plus a
ten-second heartbeat, a failure row written by the handler, and a second one
written by the Step Functions catch for the case where the handler itself is
killed. The loop now reports the time limit as an outcome (`budget: time limit
reached after 1 of 3 rule(s)`) and finalises with the rules it did finish, which
still produces a real before/after.

The general lesson: **every wait needs a bound, and every bound needs somewhere
to write when it is hit.** An agent that says "gemini gave up after 240s — quota
exhausted" is a working product with a bad afternoon. The same agent showing a
spinner is indistinguishable from one that does not work at all.

### What we would do differently

Write the diagnostic before the feature. `inspect_mirror.py`, `check_bedrock.py`
and `smoke_image.py` each took under an hour and each would have saved several
if they had existed first. Every one of them was written *after* the failure it
detects.

---

## Dashboard

```bash
cd web && npm install && npm run dev
```

Reads `out/report.json` from the CLI run by default, so it works with no AWS at
all. Point `REPORT_DIR` elsewhere, or set `NEXT_PUBLIC_API_URL` to read the
deployed API instead.

Design notes, since Best UI is judged:

- Every colour token clears 4.5:1 on the page background. An accessibility tool
  that fails its own contrast audit is indefensible.
- Rule outcome is carried by a glyph **and** a colour, never colour alone:
  `[+]` fixed, `[~]` partly fixed, `[!]` not fixed, `[?]` needs a person,
  `[x]` introduced by the agent.
- The rule list sorts regressions to the top, then unfixed, then partial, then
  deferred, then fixed. The bad news is the first thing you see.
- Focus rings are visible and deliberate; `prefers-reduced-motion` is respected;
  frames are labelled for screen readers.
- The highlight bridge is injected when the HTML is served, never written into
  the artifact. Deployed, the iframes load through `/api/page-html?src=…`,
  which fetches the presigned S3 object, injects the bridge and serves it
  same-origin — postMessage needs the same origin, and the stored artifact
  stays byte-identical to what the pipeline produced. That proxy only accepts
  `https` URLs on S3 hostnames, so it is not an open proxy.