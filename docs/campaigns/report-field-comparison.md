# Campaign Report — Field Comparison

Our [campaign report](reports.md) against the reference outbound-analytics dashboard produced by a
standalone Python script (sample dated 18 September 2026).

One table per sheet. For every field: whether our report carried it **before** this round of work,
whether their dashboard has it, whether our report carries it **now**, whether our database can
supply it, and where it lives.

**Compared on:** 18 September 2026 · **Related:** [Campaign Reports — Flow & Lifecycle](reports.md) ·
[Campaign Reports API](../apiDocs/core/campaign_report.md)

---

## Legend

| Mark | In the report columns | In "In our DB?" |
|---|---|---|
| ✓ | The field is in that report | Stored as a column — read it directly |
| ~ | — | Derivable: an aggregate, a computed rate, or a value inferred from a stored field |
| (blank) | Not in that report | — |
| ✗ | — | Not available — nothing stores it |

The three report columns:

| Column | Means |
|---|---|
| **Our report (before)** | The campaign report as it stood when this comparison was written |
| **Their report** | The reference outbound-analytics dashboard |
| **New report** | The campaign report as it stands now, after the call-detail and probing columns were added |

A row with a blank *before* and a ✓ under *New report* is something this round added. Model paths
are `Model.field`.

---

## Sheet 1 — Overview / Dashboard

| Field | Our report (before) | Their report | New report | In our DB? | Model → field |
|---|---|---|---|---|---|
| Total contacts / members | ✓ | ✓ | ✓ | ✓ | `CampaignMember` row count · `Campaign.total_members` |
| Calls attended / connected | ✓ | ✓ | ✓ | ~ | `AgentCallLog.duration_seconds` > 0 |
| Attend rate (response %) | ✓ | ✓ | ✓ | ~ | Computed: connected ÷ total calls |
| Average call duration | ✓ | ✓ | ✓ | ~ | `Avg(AgentCallLog.duration_seconds)` |
| Report generated at | ✓ | ✓ | ✓ | ✓ | `CampaignReport.completed_at` |
| Timezone of the figures | ✓ | ✓ | ✓ | ✓ | `Campaign.schedule_timezone` → `Tenant.timezone` → UTC |
| Campaign name | ✓ | | ✓ | ✓ | `Campaign.name` |
| Campaign description | ✓ | | ✓ | ✓ | `Campaign.description` |
| Campaign status | ✓ | | ✓ | ✓ | `Campaign.status` |
| Bot name | ✓ | | ✓ | ✓ | `Agent.bot_name` via `Campaign.agent` |
| Audience source | ✓ | | ✓ | ✓ | `Campaign.audience_source` |
| Launched by | ✓ | | ✓ | ✓ | `Campaign.launched_by` |
| Requested by | ✓ | | ✓ | ✓ | `CampaignReport.created_by` |
| Report ID | ✓ | | ✓ | ✓ | `CampaignReport.id` |
| Scheduled start | ✓ | | ✓ | ✓ | `Campaign.scheduled_start_at` |
| Started at | ✓ | | ✓ | ✓ | `Campaign.started_at` |
| Completed at | ✓ | | ✓ | ✓ | `Campaign.completed_at` — **never written today**, see [the known gap](../implementation-plan-scratch/campaign-lifecycle-and-billing.md#6-known-gap-nothing-sets-completed) |
| Elapsed | ✓ | | ✓ | ~ | Computed: `completed_at − started_at` |
| Callable members | ✓ | | ✓ | ~ | Computed: total − suppressed |
| Suppressed members | ✓ | | ✓ | ✓ | `CampaignMember.status` = `SUPPRESSED` |
| Suppression breakdown by reason | ✓ | | ✓ | ✓ | `CampaignMember.suppression_reason` |
| Member status breakdown | ✓ | | ✓ | ✓ | `CampaignMember.status` |
| Reach rate | ✓ | | ✓ | ~ | Computed: completed ÷ callable |
| Failure rate | ✓ | | ✓ | ~ | Computed: failed ÷ callable |
| Total calls | ✓ | | ✓ | ✓ | `AgentCallLog` via `campaign_member__campaign` |
| Total talk time | ✓ | | ✓ | ~ | `Sum(AgentCallLog.duration_seconds)` |
| Credits charged | ✓ | | ✓ | ✓ | `AgentCallLog.credits_charged` |
| Calls charged | ✓ | | ✓ | ~ | Count where `credits_charged` is not null |
| Average credits per charged call | ✓ | | ✓ | ~ | Computed |
| Credits by service tier | ✓ | | ✓ | ✓ | `AgentCallLog.service_tier` + `credits_charged` |
| Members included vs total | ✓ | | ✓ | ✓ | `CampaignReport.rows_included` · `total_rows` |
| Call direction | | ✓ | ✓ | ✓ | `AgentCallLog.direction` |
| Date / period covered | | ✓ | | ~ | `AgentCallLog.created_at` range — ours is per campaign, not per day |
| Not picked count (DNP) | | ✓ | ✓ | ~ | `AgentCallLog.duration_seconds` = 0 — an approximation; no ring-no-answer flag is stored |
| DNP rate | | ✓ | ✓ | ~ | Computed from the above |
| Max call duration | | ✓ | ✓ | ~ | `Max(AgentCallLog.duration_seconds)` |
| Total dial attempts | | ✓ | ✓ | ~ | `Sum(CampaignMember.attempt_count)` — mirrored from the runner, not per-attempt rows |
| Failed dial attempts | | ✓ | | ✗ | Nothing records an attempt that never became a call |
| Duration bucket distribution | | ✓ | ✓ | ~ | Bucketed `AgentCallLog.duration_seconds` |
| AI-handled count | | ✓ | ✓ | ~ | `AgentCallLog.human_transfer_detail` empty |
| Human-transferred count | | ✓ | ✓ | ~ | `AgentCallLog.human_transfer_detail` non-empty |
| Scheduled demos / appointments | | ✓ | ✓ | ~ | Distinct contacts with an active `Booking` created since the campaign started, via `BookingParticipant.contact` |
| Disposition breakdown | | ✓ | ✓ | ~ | `AgentCallLog.outcome` · `CampaignMember.outcome` — free text, so grouping is unreliable until outcome labels are standardised |
| Sentiment / mood breakdown | | ✓ | ✓ | ✓ | `AgentCallLog.mood` (+ `mood_confidence`) |

---

## Sheet 2 — Members / Call rows

| Field | Our report (before) | Their report | New report | In our DB? | Model → field |
|---|---|---|---|---|---|
| Name | ✓ | ✓ | ✓ | ✓ | `Contact.full_name` (`first_name` + `last_name`) |
| Phone number | ✓ | ✓ | ✓ | ✓ | `CampaignMember.phone_e164` (frozen) · `Contact.phone_e164` |
| Call log / provider call ID | ✓ | ✓ | ✓ | ✓ | `AgentCallLog.provider_call_id` · `AgentCallLog.id` |
| Call start time | ✓ | ✓ | ✓ | ✓ | `AgentCallLog.started_at`, beside `CampaignMember.last_call_at` in its own column |
| Call duration (seconds) | ✓ | ✓ | ✓ | ✓ | `AgentCallLog.duration_seconds` |
| Outcome / disposition | ✓ | ✓ | ✓ | ✓ | `CampaignMember.outcome` · `AgentCallLog.outcome` |
| Member ID | ✓ | | ✓ | ✓ | `CampaignMember.id` |
| Company | ✓ | | ✓ | ✓ | `Contact.company` |
| Member status | ✓ | | ✓ | ✓ | `CampaignMember.status` |
| Suppression reason | ✓ | | ✓ | ✓ | `CampaignMember.suppression_reason` |
| Attempts | ✓ | | ✓ | ✓ | `CampaignMember.attempt_count` |
| Credits charged | ✓ | | ✓ | ✓ | `AgentCallLog.credits_charged` |
| Error message | ✓ | | ✓ | ✓ | `CampaignMember.error_message` |
| Serial number | | ✓ | | ~ | Generated by the writer as it streams rows — nothing to store |
| Call from (our number) | | ✓ | ✓ | ✓ | `AgentCallLog.caller_number` |
| Call to (dialled number) | | ✓ | ✓ | ✓ | `AgentCallLog.callee_number` (fallback: `AgentCallLog.customer_number`) |
| Bot / campaign per row | | ✓ | ✓ | ✓ | `Agent.bot_name` · `Campaign.name` |
| Call end time | | ✓ | ✓ | ✓ | `AgentCallLog.ended_at` |
| Attended by (AI or human) | | ✓ | ✓ | ~ | Inferred from `AgentCallLog.human_transfer_detail` |
| Sentiment / mood | | ✓ | ✓ | ✓ | `AgentCallLog.mood` |
| Demo date & time | | ✓ | ✓ | ~ | `Booking.start_time` via `BookingParticipant.contact` |
| Callback date & time | | ✓ | | ✗ | No date is stored — `AgentCallLog.follow_up_detail` is free text |
| Recording link | | ✓ | ✓ | ✓ | `AgentCallLog.external_recording_url`, else `AgentCallRecording.file` signed for the report's lifetime |
| Detected language | | ✓ | ✓ | ✓ | `AgentCallLog.preferred_language` |
| AI confidence | | ✓ | | ~ | Only `AgentCallLog.mood_confidence`, which ships as **Sentiment confidence** — confidence in the mood reading, not in the call overall |
| Source quote | | ✓ | | ~ | Inside `AgentCallLog.transcript`; not a field of its own |
| AI summary | | ✓ | ✓ | ✓ | `AgentCallLog.ai_summary` |
| Chat history / transcript | | ✓ | ✓ | ✓ | `AgentCallLog.transcript` |
| Processed at | | ✓ | ✓ | ✓ | `AgentCallLog.created_at` (`updated_at` for the last change) |
| Probing question — answer summary † | | | ✓ | ✓ | `AgentCallLog.probing_questions[].answerSummary` |
| Probing question — positive? † | | | ✓ | ✓ | `AgentCallLog.probing_questions[].isPositive` |
| Probing questions asked (count) | | | ✓ | ✓ | `AgentCallLog.probing_questions_count` |

† **Two columns per question**, XLSX only, discovered per report — see
[Probing questions as columns](#probing-questions-as-columns).

---

## Probing questions as columns

Every call log already records what the agent's probing questions produced, on
`AgentCallLog.probing_questions` (JSONB), with `AgentCallLog.probing_questions_count` beside it:

```json
[
  {"question": "how do you manage your leads?", "isPositive": false, "answerSummary": "Not answered during the call."},
  {"question": "from which industry you are?", "isPositive": false, "answerSummary": "Not answered during the call."},
  {"question": "what is your team size for manage the leads?", "isPositive": false, "answerSummary": "Not answered during the call."}
]
```

**Built.** The writer is `probing_columns_for` in `core/services/campaign_reports.py`, and every
behaviour below is covered by `core/tests/campaigns/test_campaign_report_probing.py`.

### What lands in the workbook

Each distinct question becomes **two columns**, at the right-hand end of the Members sheet:

| Column | Holds | Example |
|---|---|---|
| `Q1 — how do you manage your leads?` | `answerSummary`, verbatim | `Not answered during the call.` |
| `Q1 positive?` | `isPositive` | `Yes` / `No` |

One pair per question, shared by every member: a member whose call covered that question fills it,
a member whose call never asked it leaves it blank. A question that turns up in a call with no
column yet is appended at the end, so the set of pairs only ever grows.

### Where the column list comes from

The list must be complete **before the first row is written** — the workbook is streamed in
append-only mode, and a column cannot be added once the header row is out. So the writer does a
pre-pass per report, before streaming:

1. **The agent's configured questions**, in priority order — the expected set, and one small query:
   `campaign.agent.probing_questions.order_by("priority")` on `ProbingQuestion`.
2. **Anything else that appears in this campaign's call logs**, appended in first-seen order. One
   aggregate query:

```sql
SELECT DISTINCT elem->>'question' AS question
FROM core_agent_call_log log
JOIN core_campaign_member m ON m.id = log.campaign_member_id
CROSS JOIN LATERAL jsonb_array_elements(log.probing_questions) elem
WHERE m.campaign_id = %s
  AND log.is_deleted = false
  AND jsonb_typeof(log.probing_questions) = 'array';
```

The `jsonb_typeof` guard matters: one row whose value is not an array would otherwise fail the
whole query.

Two extra queries per report, not per row — the streaming loop costs the same as it does today.

### Matching questions to columns

Questions are matched **by their text**, which has two consequences:

- Compare on a normalised form — trimmed, inner whitespace collapsed, case-folded — and display the
  first spelling seen.
- **Editing a question on the agent creates a new column.** Older call logs keep the old wording, so
  both appear side by side. That is honest; matching by id instead would need the runner to send an
  id, which it does not today.

### Reading the values

| Case | Answer column | Positive column |
|---|---|---|
| Asked and answered | The summary text | `Yes` or `No` |
| Asked, not answered | `Not answered during the call.` | `No` — see below |
| Not in this call's array | blank | blank |

> **`isPositive: false` means two different things.** It covers "answered, and the answer was
> negative" *and* "never answered" — in the sample above every question is unanswered and every flag
> is false. Only the summary text separates them, and that text is free-form. If the distinction
> matters for reporting, the runner has to send an explicit `answered` flag: a contract change, not
> something to infer.

### Which call's answers

The same call the rest of the row comes from — the member's **latest** call log — so the answers
line up with Duration, Credits charged and Provider call ID.

The alternative is merging across attempts, taking the first non-empty answer per question, so an
answer given on attempt 1 is not lost when attempt 3 goes unanswered. Better data, but the row then
mixes calls and "latest call" stops being true of it as a whole. Worth deciding before building.

### XLSX only, not the PDF

The Members sheet has 13 columns today; ten questions would add twenty more. The workbook carries
that fine — the frozen header and autofilter keep it usable — but the PDF's landscape table is
already at its width limit with 13. **The probing columns are XLSX only**; the PDF is unchanged.

### Decisions taken

| Question | Decision |
|---|---|
| Latest call, or merged across attempts? | **Latest call** — the answers line up with Duration, Credits and Provider call ID in the same row |
| Include configured questions no call ever asked? | **Yes** — an empty column says nobody got that far |
| `Yes`/`No`, or `TRUE`/`FALSE`? | **`Yes`/`No`**, matching the rest of the sheet |
| Cap the number of pairs? | **`CAMPAIGN_REPORT_MAX_PROBING_QUESTIONS`**, default 25; `0` turns the columns off |
| Add `probing_questions_count` as a plain column? | **Yes** — column 14, in both formats |
| Add a per-question positive rate to the overview sheet? | **Not yet** — a natural second step now the columns exist |

**Still open, and a runner-contract change:** `isPositive` cannot separate "answered negatively"
from "never answered". An explicit `answered` flag in the payload would fix it.

---

## The structural difference

The two reports count different things, and this matters more than any single column.

| | Our report | Their dashboard |
|---|---|---|
| A row is | One **person** in one campaign | One **call** |
| Scope | One campaign | A day, across everything |
| People never called | Included, with a suppression reason | Absent — no call, no row |
| Someone called three times | One row, `Attempts` = 3, latest call's figures | Three rows |

So "attempts" is a column for us and extra rows for them, and our suppression record has no
equivalent on their side. Neither shape is wrong; they answer different questions — *"what happened
to this campaign's audience"* versus *"what did the bot do today"*.

---

## What we could add, and what needs new data

**Added.** Every row with a blank *before* and a ✓ under *New report* — which is every field of the
reference dashboard except the seven below:

- **Sheet 2** — the call's own record: call from and to, bot, start and end times, attended by,
  sentiment and its confidence, detected language, appointment booked, follow-up note, recording
  link, AI summary, transcript, logged at. Plus the probing question pairs.
- **Sheet 1** — direction, not picked up and its rate, longest call, attempts reported, the four
  duration buckets, bot-handled versus transferred, appointments booked, and the sentiment and
  disposition breakdowns.

All of it in the **workbook only**: the PDF keeps the fixed columns, because its landscape table is
already at its width limit.

### Still missing, and why

| Field | Why not |
|---|---|
| Serial number | Excel numbers its own rows. A column for it is a spreadsheet imitating a screen |
| Date / period covered | Ours is one campaign, not one day. The campaign's own dates are in the overview |
| Failed dial attempts | Nothing records an attempt that never became a call; `attempt_count` is what the runner reports having tried |
| Callback date & time | No date is stored. The free text is in **Follow-up note**, as sent |
| AI confidence | Only confidence in the sentiment reading exists, and it ships as **Sentiment confidence**. Call-level confidence is not recorded |
| Source quote | Would have to be guessed out of the transcript, which is itself a column |
| Not picked up, exactly | Approximated by a zero-second call — there is no ring-no-answer flag |

Four of those are the same ask of the runner: **an explicit `answered` flag on probing questions, a
ring-no-answer signal, per-attempt dial records, and a structured callback time.** None is writer
work; all four are contract changes.

**Needs a decision first.** The disposition breakdown is only as good as its labels: `outcome` is
free text today, so any grouping is guesswork until
standard outcome labels land.

**Needs new data.** Failed dial attempts (nothing records an attempt that never became a call),
callback date and time (free text today), AI confidence for the call as a whole, and an explicit
ring-no-answer flag — today DNP can only be approximated by a zero-second call.

**Unclear in the reference sheet.** `TOTAL = 4000` and `FAILED = 3422` do not reconcile with 578
contacts, so they are recorded above as dial attempts — a guess. Confirm what they count before
copying them.
