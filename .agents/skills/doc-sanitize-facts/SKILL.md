---
name: doc-sanitize-facts
description: Convert project documentation into a verifiable-facts-only format — strip ADR/decision-record rationale, acceptance criteria, user-story/sprint/ticket-narrative filler, dated status markers, and forward-design speculation, leaving only what is true of the current app and checkable against the code. Use this whenever the user asks to clean up, factualize, "de-ADR", or "de-story" docs, remove "AC"/"acceptance criteria"/"story" filler, make documentation reflect current state, or audit docs for staleness — and apply it by default when writing or rewriting any reference doc in this repo so it starts facts-only. Reach for this instead of ad-hoc doc editing whenever documentation should describe what the app does now rather than why it was decided.
---

# Facts-Only Docs

Turn a doc into a description of **what the app does now**, verifiable against the
code — not a record of why decisions were made, what tickets drove them, or what
might happen later.

## When to use

- The user asks to clean up, factualize, "de-ADR", or "de-story" documentation,
  or to remove acceptance-criteria / user-story / sprint / ticket filler.
- The user wants docs to "reflect the current state" or asks whether a doc is
  stale.
- You are writing or rewriting any reference doc in this repo — start it
  facts-only by default.

## Why facts-only

A reader (human or agent) consults a doc to learn how the system behaves so they
can change it safely. Rationale, alternatives-considered, ticket numbers, and
delivery plans don't help with that and they rot fastest — a decision record
written months ago describes a system that has since moved on. Git already
preserves the "why" and the history; the working-tree doc should carry the
current truth. Keeping docs to checkable facts makes them cheap to trust and
cheap to keep correct.

## Step 1 — Classify the doc

| Type                       | Signals                                                                 | Action                                                                 |
| -------------------------- | ----------------------------------------------------------------------- | ---------------------------------------------------------------------- |
| **Reference / runbook**    | Current behavior, file/module layout, how-to steps, config tables       | Light cleanup — strip dated/status framing, keep the facts & procedures |
| **ADR / decision record**  | `Decision.`, `Why`, `Tradeoff`, `Anti-pattern`, prior-art surveys       | Rewrite to current-state facts; drop the rationale                     |
| **Project-tracking artifact** | User stories, acceptance criteria, delivery waves, audit-on-DATE logs | Extract any still-true facts into the right reference doc, then delete  |

## Step 2 — Verify before you write (do not trust the old doc)

Docs drift. Treat the existing text as a *claim*, not a fact, and confirm each
retained statement against the current code/config before keeping it:

- Open the file, `grep` the symbol, confirm the endpoint/route/flag still exists.
- When the doc and the code disagree, the **code wins**. In this repo the sibling
  repos are the source of truth (`../sps-agent-ui`, `../sps-agent-api`) and
  AGENTS.md says follow the real server over the generated spec.
- Watch for confidently-stated staleness: an ADR here claimed "only the WebSocket
  client exists" long after a full REST layer had landed. If you had copied that,
  the "facts-only" doc would have been confidently wrong.

If a fact can't be verified, leave it out rather than guess.

## Step 3 — Strip vs. keep

**Remove:**

- Rationale: `Decision.`, `Why`, `Tradeoff`, `Anti-pattern`, "prior art",
  alternatives-considered, vendor/tool comparison surveys.
- Status & dates: `Status:`, `ratified`, `PoC`, "as of <date>", "audited on
  <date>", point-in-time snapshots.
- Tracking refs: ticket IDs used as narrative (`MaxChat/NN`, `Intents/…`),
  `Tier-N` tags, sprint/wave/priority, estimates, t-shirt sizes.
- Story/AC scaffolding: "As a user…", `Acceptance criteria`, Given/When/Then.
- Narratives: incident/debugging war-stories ("a session cost an afternoon"),
  "open questions", "follow-ups", delivery plans.
- Forward-design speculation. Keep at most a one-line current-state fact, e.g.
  "the backend sender is a stub that logs and returns false" — not a plan for
  what it will become.

**Keep (and verify):**

- Current behavior, module/file layout, exported functions.
- Endpoints, methods, paths, status codes, payload/frame shapes.
- Config values, environment mapping, allow-lists/enums.
- Procedures (how-to steps), constraints that are still true.

## Step 4 — Conventions

- **Anchor facts to source.** Link the file with a relative path and name the
  real symbol, so a reader can check the claim: `[http.ts](../rn/api/http.ts)`.
- **Links must work on GitHub *and* in local preview.** Relative file paths
  resolve the same in both. Do **not** use terminal-style line suffixes
  (`file.ts:42`) — they break on GitHub. Avoid `#L42` anchors too; they drift as
  code moves. Link the file and let the reader search.
- **Cross-reference docs by section *title*, not number.** A code comment or doc
  that points at "§5" dangles the moment sections are reordered; "the Errors
  section of docs/api-architecture.md" survives. When converting, fix any
  existing `§N` references you find (in code comments and other docs) the same
  way.
- **Add a table of contents** to longer docs (roughly >100 lines). Link only
  top-level section headings, and use GitHub's slug rules (lowercase, drop
  punctuation, spaces → hyphens). Skip headings containing em-dashes or other
  punctuation in the TOC — their slugs are quirky and differ across renderers.
- **Prefer tables** for fact lists, concise prose for explanation, and imperative
  voice for procedures.

## Step 5 — Project-tracking artifacts: extract, then delete

A pure ticket/audit/plan doc isn't about the app's current state, so it doesn't
become a facts-only doc — it gets dissolved:

1. Identify any still-true facts it contains (e.g. a real protocol comparison).
2. Move those into the appropriate reference doc.
3. Update the docs index and any links pointing at the file.
4. Delete the file.

Confirm deletion with the user if it isn't already covered by their instruction.

## Step 6 — Finish the job

- Update the docs index (`docs/README.md`): retitle/relabel converted docs, drop
  obsolete "ADR" labels and any "append dated revisions, don't rewrite history"
  convention that the new format contradicts, and add/remove rows for
  created/deleted docs.
- `grep` for now-dangling references to anything you changed or deleted —
  `§N` citations, links to removed files — and fix them.
- Optionally run the scanner (below) again to confirm the filler signals are
  gone.

## Scanner

`scan.sh` flags docs that still carry filler signals and finds broken/terminal
links, so you can target the conversion and verify it afterward:

```
bash .Codex/skills/facts-only-docs/scan.sh docs
```

Pass a directory (default `docs`). It reports, per file, which filler patterns
hit, plus any relative links whose target is missing or that use a `:line`
suffix. Use it to build the work list in Step 1 and to confirm a clean result in
Step 6. It is a signal, not a verdict — a hit isn't always filler, and a clean
file can still need verification against code.

## Example

**Before (ADR):**

> ## 5. Error class hierarchy
> **Decision.** Errors follow a typed class hierarchy. **Why:** `instanceof` is
> checked by TS… **Enterprise prior art:** OpenAI and Anthropic SDKs are
> identical here… **Status:** ratified 2026-05-28.

**After (facts-only, verified against `rn/api/http.ts`):**

> ## 5. Errors
> The REST helpers in [http.ts](../rn/api/http.ts) throw a plain `Error` with the
> message `${label} failed: HTTP ${status}`. There is no typed error class
> hierarchy in the API layer. (Authentication has its own error types in
> [authErrors.ts](../rn/auth/authErrors.ts).)