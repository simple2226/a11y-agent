# Contributing

Thanks for taking the time to contribute. This doc covers how to get the project
running locally, what a good change looks like, and how to get it merged.

## Getting started

1. Fork the repo and clone your fork.
2. Create a branch off `main`:

   ```bash
   git checkout -b feat/short-description
   ```

3. Install dependencies using whatever the project's package manager is
   (`npm install`, `pip install -r requirements.txt`, `cargo build`, etc.).
4. Confirm the test suite passes *before* you change anything, so you know any
   failure afterwards is yours.

## Branch naming

| Prefix      | Use for                                        |
| ----------- | ---------------------------------------------- |
| `feat/`     | New functionality                              |
| `fix/`      | Bug fixes                                      |
| `refactor/` | Behaviour-preserving restructuring             |
| `docs/`     | Documentation only                             |
| `chore/`    | Tooling, CI, dependencies, repo hygiene        |
| `test/`     | Adding or fixing tests only                    |

## Commit messages

Follow [Conventional Commits](https://www.conventionalcommits.org/):

```
<type>(<optional scope>): <short imperative summary>

<optional body explaining *why*, not *what*>
```

Examples:

```
fix(auth): reject expired refresh tokens instead of silently renewing
chore: add .editorconfig and .gitattributes
```

Rules of thumb:

- Summary line under 72 characters, imperative mood ("add", not "added").
- One logical change per commit. If you need "and" in the summary, split it.
- The body explains the reasoning. The diff already explains the mechanics.

## Code style

- Match the surrounding code. A consistent codebase beats your personal
  preference.
- Name things for what they are. Single-letter variables are fine for loop
  indices and nothing else.
- Don't reformat files you aren't otherwise touching — it buries the real diff.
- `.editorconfig` handles indentation and line endings; make sure your editor
  respects it.

## Tests

- New behaviour needs a test. Bug fixes need a test that fails before the fix.
- Keep tests deterministic: no reliance on wall-clock time, network access, or
  ordering between test cases.
- Prefer a few clear assertions over one giant snapshot.

## Opening a pull request

1. Rebase onto the latest `main` and resolve conflicts locally.
2. Push your branch and open a PR against `main`.
3. Fill out the PR template. "Fixes #123" in the description auto-closes the
   issue on merge.
4. Keep the PR focused. A 40-line PR gets reviewed today; a 2,000-line PR gets
   reviewed eventually.

### Review

- Expect at least one round of comments. It isn't personal.
- Push follow-up commits rather than force-pushing mid-review, so reviewers can
  see what changed. Squash at merge time.

## Reporting bugs

Open an issue using the bug report template. A useful report includes what you
did, what you expected, what actually happened, and the environment it happened
in. A stack trace beats a description of a stack trace.

## Questions

If something here is unclear or out of date, open an issue — that's a valid
contribution too.
