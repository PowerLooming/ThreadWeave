# Capture quality

This is the measurement half of the second pilot condition: *capture-quality
measurement, decisions found versus missed, sampled weekly from the pilot team*.
It exists so the pilot can answer one question with a number instead of an
impression: of what ThreadWeave stored this week, how much was knowledge, and
how much was noise.

`scripts/capture_quality_sample.py` prints a sheet. A person marks it up. The
script never scores its own output, because the detector's labels are the thing
being checked.

## The number that gets reported

**Capture precision**, for one wing, for one week:

```
precision = K / (K + N)
```

`K` is the count of sampled entries a human judged knowledge worth keeping, `N`
the count judged noise, and `?` the count they could not judge. A `?` sits in
neither side of the fraction and is reported next to it, because a sheet full
of question marks is not a good week, it is an unreadable one.

Reported alongside it, because the fraction alone hides the other half:

- **volume**, the number of entries captured that week. Ten perfect entries and
  four hundred perfect entries are different products.
- **the miss sample**, how many of the two or three decisions the team named
  themselves were found in the memory. Precision says nothing about what was
  never captured, and a system can be precisely useless.

What is not reported: the detector's own decision/answer counts. They appear on
the sheet for comparison only.

## The ten-minute protocol

One person from the pilot team does this, on the same weekday each week, at the
end of the week being measured. The same person each week matters more than
which person, because two people mark differently.

| # | step | time |
|---|---|---|
| 1 | Print the sheet for last week | 1 min |
| 2 | Mark every box K, N or ? | 6 min |
| 3 | Fill the tally, per wing and overall | 1 min |
| 4 | Name two or three decisions the team made last week and check them | 2 min |
| 5 | Send the sheet and the tally to the pilot owner | 0 min |

**1. Print the sheet.** Last week's range, five entries per wing:

```
python scripts/capture_quality_sample.py \
    --since 2026-10-16 --until 2026-10-22 \
    --per-wing 5 --out capture-quality-2026-10-22.txt
```

`--store` selects the entry database when it is not the default
(`~/.threadweave/entries.sqlite3`, or `THREADWEAVE_ENTRY_DB`); a PostgreSQL
instance takes the full URL. `--wing pilot` narrows the sheet to one wing. If
the team has no shell access, the owner runs this step and hands over the file;
marking never needs the store.

The dates are inclusive and read as UTC days. `--since 2026-10-16 --until
2026-10-22` covers the seven days from the 16th up to and including the 22nd.

**2. Mark every box.** One letter per entry, in the order they appear. The
rubric below is the whole instruction; nothing else needs reading.

**3. Fill the tally.** Per wing and overall, then the fraction. The sheet prints
a blank tally table at the bottom for exactly this.

**4. Name the misses.** Precision only measures what was stored. Write down two
or three concrete decisions the team actually made that week (a switch of
supplier, a date moved, a policy settled), then search for each one:

```
threadweave search "supplier direct q4"
```

Record found or not found. This is a sample of recall, not a rate: three probes
a week over four weeks is twelve probes, which is enough to see a connector that
captures nothing.

**5. Send it.** The sheet, the tally, the miss list. Who receives it and where
the weekly rows are kept is the pilot's own arrangement; the record table below
is the shape of that record.

## The marking rubric

Mark the entry as it stands. Do not improve it, do not fix a typo, do not mark
what it could have been if someone had written it better. The question is one
question: **would the team miss this in three months if it vanished?**

| mark | means | examples |
|---|---|---|
| `K` | knowledge worth keeping | a decision and its reason; how a process actually works; a caveat that changes how someone acts; a correction to an earlier belief; a commitment with who and when |
| `N` | noise | "thanks", "will do", "morning all"; a bare question with no answer attached; a link with no explanation; an automated notice, calendar blurb, newsletter, timesheet reminder, NDR; a status line with no content ("done", "moved to review") |
| `?` | cannot tell from the excerpt | the excerpt is truncated before the substance, or it is ambiguous without the thread it came from |

Tricky cases, decided once so they stay decided:

- A question that carries its answer or the context that produced it is `K`.
  "Does anyone know where the escrow checklist lives?" is `N`. "Where does the
  escrow checklist live? It moved to the finance room after the audit." is `K`.
- A duplicate of an entry the memory already holds is `N`, and write `dup` next
  to it. Duplicates are a dedup defect, not knowledge.
- Anything that should never have been stored (personal data, a personal attack,
  someone's health, a salary figure) is `N`, and flag it. The PII and gossip
  gates are meant to stop that, so a flag is a defect report, not a marking
  opinion.
- An entry from outside the team's work (a vendor's marketing mail that got
  through) is `N`, and worth noting: it is a source-filter gap.
- A wrong decision that the team later reversed is still `K`. The memory is
  supposed to hold what was decided, including decisions that did not hold.

Rules that keep weeks comparable:

- Two or three `?` marks in one sheet are a signal about the previews, which is
  useful. Ten `?` marks means the sheet is not being read carefully.
- Do not skip entries. An unmarked sheet cannot be scored, and a partial one
  cannot be compared with last week.
- Do not re-run the sheet after marking and expect the same lines in a different
  order: the sample is fixed for a store and a date range, but a new capture
  inside the range changes the selection. Measure a closed week.

## Recording the week

One row per week per wing, kept where the pilot team keeps its notes:

| week | wing | captured | sampled | K | N | ? | precision | decisions named | found |
|---|---|---|---|---|---|---|---|---|---|
| 2026-10-12 to 2026-10-18 | pilot | 41 | 5 | 4 | 1 | 0 | 0.80 | 3 | 2 |
| 2026-10-12 to 2026-10-18 | adjacent | 27 | 5 | 2 | 2 | 1 | 0.50 | 3 | 3 |

A worked example, not a real measurement: the pilot wing is precise and misses
one decision, the adjacent wing captures half noise. Both rows are worth
keeping. The second one is the useful one.

## Reading four weeks of numbers

- **Precision at or above 0.8**, stable across two weeks: the capture is doing
  its job for that wing. Nothing to change.
- **Between 0.6 and 0.8**: look at the `N` marks that repeat. The usual causes
  are one noisy source (a notification mailbox, a system account) and one room
  that collects chatter.
- **Below 0.6**: stop widening the pilot. Either the wing is capturing the wrong
  sources, or the detector threshold is too low for that team's mail.
- **Volume collapsing to near zero** with everything else unchanged: the
  connector stopped, not the team. Check `daemon status` and the last poll time
  before drawing any other conclusion.
- **Misses found below half of the named decisions**: the capture is not seeing
  the places the team's decisions are actually made. That is a connector or
  wing-mapping question, not a threshold one.

Four weeks of these rows are the evidence for the pilot condition. The number
that goes in front of the IT manager is the precision, per wing, with the volume
and the miss sample beside it, and the four marked-up sheets attached.

## Why the sheet is deterministic

The script sorts each wing by capture time, then by entry id, and takes entries
at even intervals across that order, so the sample spreads over the week instead
of clustering at the first day. The same store and the same closed date range
always print the same sheet, which is what lets a marked-up sheet be filed as
evidence and a second run be diffed against it. No model is involved anywhere in
the measurement: the only judgement in the loop is the person with the pen.
