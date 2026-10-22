# Console copy: one short line, no teaching

**Operator ruling, 2026-09-09**, looking at the deployed console with an
overlay tool on it: *"i think there is still a lot to change, i see a lot of AI
slop"*, and *"that is across the whole platform"*. Asked how hard to cut, the
answer was **"One short line, no teaching"**.

## What was wrong

Documentation was written into the interface. Measured on `f0667f1`, across
`description` / `body` / `subtitle` props in `src/modules` and `src/shell`:

| length | count |
| --- | --- |
| over 80 chars | 159 |
| over 120 chars | 107 |
| over 200 chars | 40 |
| longest | **465** (`/agent-push/authorizations` page description) |

Each sentence is defensible alone. Together they read as an essay pasted into
a product. The real `/device-groups` subtitle was 183 characters and taught
the reader the precedence model before they had seen a group.

## The rule

1. **A page description says what the page IS. 40–70 characters.** Not how it
   works, not what the rules are, not what happens next.
   - Before: *"A group is the fleet, carved up. A deployment, a patch, backup
     or remote policy, a health profile and a role grant all take a group;
     devices in no group are governed by the organisation default."* (183)
   - After: *"Carve the fleet into groups for policy and deployment."* (53)

2. **Delete anything the page already shows.** The `/device-groups` table has
   a "No group" row reading "Governed by the organisation default policy". The
   subtitle said it again in longer words. A sentence that restates a visible
   row is not information, it is noise.

3. **An empty state is one sentence and an action.** Not a paragraph
   explaining the domain model.

4. **A `.notice` is a warning or a consequence, not a lesson.** If it explains
   a mechanism, it belongs in `/docs`.

5. **Never explain the platform's architecture in page chrome.** "This
   platform never connects to a machine…" is a docs page.

## What must NOT be lost

A load-bearing fact — one an operator would be *surprised* by, and that the
page does not otherwise show — is kept, in its own place, not smuggled back
into the subtitle. Where such a fact exists and has nowhere to go, say so in
the report rather than deleting it silently. Security-relevant statements
(what a grant permits, what a destructive action does) are NOT decoration and
are not cut to fit a character count: they move to the control they describe,
or to the confirmation dialog, where they are read at the moment they matter.

## Ruled on, not open: `--t-cap` stays at 10.5px

The overlay flagged "undersized functional text" on almost every page, and it
was one thing every time: the `--t-cap` rung at **10.5px**, used for tile
captions, table column headers and rail section labels.

**The operator saw it rendered both ways and chose 10.5px** (2026-09-09). The
question was put with two screenshots of `/devices` differing in nothing else.

This is a legibility preference, not an accessibility failure, and the
distinction is the reason it was the operator's call rather than a defect to
fix: every `--t-cap` pair is graded at 4.5:1 in `contrast.test.ts`, and the
browser pass samples those labels as composited pixels across six viewports.
Both were green when the ruling was made.

**Do not re-raise this from a tool's output.** A scanner that flags small text
by size alone cannot see the contrast grading behind it, and this one will keep
flagging it on every run.

## Also decided against, from the same overlay, with the measurement

- **"Low contrast text" on inactive tabs** — they are `--muted` `#606a7a`,
  which measures **5.4:1** on `--surface` and about 5.1:1 on `--bg`. The
  threshold is 4.5:1. `contrast.test.ts` grades exactly these pairs. A false
  positive; the colour was not darkened to satisfy it.
- **"Overused font: inter (100% of text)"** — one typeface across a product is
  a deliberate choice.
- **"Layout property animation: transition: width"** — it is on `.sidebar`,
  which already sits inside the `prefers-reduced-motion: reduce` block where it
  is cut to `.01ms`, and it fires once per rail-collapse click. Rewriting it as
  a transform would mean animating the content's `margin-left` in lockstep to
  avoid a visible tear: a regression risk on every page, for 0.22s of layout on
  an infrequent, already-opt-out-able interaction.
