# Campaign Reports — Implementation Plan

Build the on-demand campaign report described in
[the feature detail](../feature-detail-scratch/campaign-report.md): a request that
returns immediately, a worker that builds a file, and a notification when it lands.

**Related:** [Campaign Module](../campaigns/README.md) ·
[Contact Import](../apiDocs/core/contact_import.md) — the closest existing precedent

---

## Contents

0. [Prerequisite — credits on the call log](#0-prerequisite--credits-on-the-call-log)
1. [What already exists](#1-what-already-exists)
2. [Design decisions](#2-design-decisions)
3. [The model](#3-the-model)
4. [File-by-file changes](#4-file-by-file-changes)
5. [The generation task](#5-the-generation-task)
6. [Memory and query shape](#6-memory-and-query-shape)
7. [Expiry and the stuck-job sweep](#7-expiry-and-the-stuck-job-sweep)
8. [Test plan](#8-test-plan)
9. [Phasing](#9-phasing)
10. [Out of scope](#10-out-of-scope)

---

## 0. Prerequisite — credits on the call log

**Build this before anything else in this plan.** The report needs "credits used" both
as a campaign total and per member, and the path to that number today is broken in a way
that matters beyond reports.

### The bug being fixed

Credits reach a call through a three-hop path ending in a **string** match:

```
CampaignMember → AgentCallLog.provider_call_id → CreditLedger.reference_id
```

The last hop is not an equality. When a charge draws from both buckets, `consume()`
writes **two** ledger rows, and the second is keyed `{provider_call_id}#purchased` —
which is why `refund()` deliberately looks for both:

```python
reference_id__in=[reference_id, f"{reference_id}#{Bucket.PURCHASED}"]
```

Any join written as `l.reference_id = c.provider_call_id` therefore **silently
undercounts** for a tenant with purchased credits. Nothing flags it; the number just
comes out low and plausible.

> This trap is not confined to reports. Anything joining ledger rows to calls has it,
> and it is worth a comment on `CreditLedger.reference_id` saying so.

### The fix — denormalise the total onto the call log

Add `credits_charged` to `AgentCallLog`, written at charge time from the rated total,
which is computed **before** the bucket split and so never has the suffix problem.

| Change | File |
|---|---|
| `credits_charged` — `PositiveIntegerField(null=True)`. Null means never charged, `0` means charged nothing — a distinction the report shows | `core/models/agent_call_log.py` |
| Migration `0059` — `AddField` only. No index: the column is summed, never filtered on | `core/migrations/` |
| `consume_detailed()` returns the rated total; `consume()` wraps it, unchanged | `wallet/services/wallet.py` |
| `AgentCallLog.record_credits_charged()` — the one place the value is written | `core/models/agent_call_log.py` |
| `_charge_credits` and the admin retro-charge both call it | `core/views/agent_call_log.py`, `wallet/admin/wallet.py` |
| Backfill for existing rows | `wallet/management/commands/backfill_call_credits.py` |
| Tests | `core/tests/billing/test_call_credits_charged.py` |

**Null, not zero, as the default.** "Never charged" and "charged zero" are different
facts — the first is an unrated call the sweep should be reporting, the second is a
legitimate free call. A default of `0` merges them and the report can no longer tell a
reader which happened.

### `consume_detailed()`, and why `consume()` keeps its return

`consume()` returns `entries[0]`, whose `.amount` is only the **first bucket's** share,
so a caller cannot learn the total from it.

An earlier draft of this section changed `consume()` to return a result object. **As
built, it does not.** `consume_detailed()` returns `ConsumeResult(credits, entries)` with
an `.entry` property, and `consume()` is a one-line wrapper that returns `.entry`.

The tests pinned the old return far harder than production code did: `.amount`,
`.operation`, `.units`, `.metadata` and `assertIsNone` on replays, across ~15 tests in
`wallet`, `onboard` and `ops`. The wrapper leaves every one untouched, and the total
comes from `result.credits` explicitly rather than being re-derived from a row.

(It *could* be re-derived — `_post_debit` copies the full rating onto every row, so
`units × rate_applied` equals the whole charge. That silently depends on the pricing
formula in `rate_quantity`, and the first discount or credit floor added there would
break it with no error. Not relied on.)

Three details that are easy to undo by accident:

- **`consume()` is not itself atomic.** `consume_detailed()` is. A second transaction
  would add a savepoint to every charge, and the query-budget tests count statements.
- **A zero-credit charge returns a result, not `None`** — `ConsumeResult(credits=0)` with
  no entries. That is what lets a free call record `0` instead of the null that means
  "never charged". `consume()` still returns `None` for it.
- **`test_cc08` mocks by name.** It patched `consume`, which the endpoint no longer
  calls; left alone it would have kept passing while intercepting nothing. Retargeted
  to `consume_detailed`.

| Caller | Calls | On failing to record the total |
|---|---|---|
| `_charge_credits` (call-log POST) | `consume_detailed` | Logged in its own `try`, distinct from "failed to charge" — the charge landed |
| `charge_unrated_calls` (wallet admin) | `consume_detailed` | Surfaced to the operator as a warning |

Both write through `AgentCallLog.record_credits_charged()`, a queryset update, so the
pre_save contact resolution does not run twice for one row.

### Refunds, and what the number means

`credits_charged` records **what was charged**, not what was net of refunds. A refunded
call keeps its figure.

That is the honest reading for a campaign report — "this call cost 949 credits" stays
true after somebody refunds it — but the column must be **labelled `credits_charged`,
never `credits_used`**, or a reader will reasonably assume it is net.

The campaign overview total is computed the same way, from the same field, so the sheet
is internally consistent. A tenant who wants net figures reads the wallet ledger, which
is the only place that answer lives.

### Backfill

Existing call logs have no value. One command, run out of band:

- Match ledger rows by `reference_id__in=[id, f"{id}#purchased"]` — **both keys**, which
  is exactly the bug this whole section exists to avoid repeating.
- Sum `abs(amount)` across them, write it to the log.
- Leave `credits_charged` null where no deduction exists: those are genuinely unrated
  calls, and inventing a `0` would hide them from the sweep that reports them.

Kept out of the migration for the same reason `backfill_call_logs` is — a slow backfill
must not block a deploy.

### Tests

`core/tests/billing/test_call_credits_charged.py`, built on `ConsumptionTestCase`:

- A charge spanning both buckets records the **full** total, while its primary ledger
  row holds only the allocated share. *The regression test for the bug; it should have
  existed already.*
- A single-bucket charge records its amount.
- Zero duration, a short wallet and a failing wallet all leave `credits_charged`
  **null**; a free call records **0**. Both halves of the null/zero rule.
- Failing to *record* the total does not fail the POST, and is logged as a recording
  failure — never as "failed to charge", because the charge landed.
- `consume_detailed()` carries the total, every row and the primary entry; `consume()`
  still returns a ledger row; a replay is `None` from both.
- The backfill fills historical rows including a split charge, leaves uncharged calls
  null, and changes nothing under `--dry-run`.

Plus `test_cc08`, retargeted from `consume` to `consume_detailed`.

**Backfill limitation.** A historical free call wrote no ledger row, so the backfill
cannot tell it from a call that was never charged, and leaves it null. Only calls
charged after this ships can record `0`.

---

## 1. What already exists

Almost every piece of this is a pattern the codebase already uses. That is the point of
this section — the plan is mostly assembly, not invention.

| Need | What to reuse |
|---|---|
| A long job with a status a user watches | `ContactImportSession` — status enum, `OPEN_STATUSES`/`TERMINAL_STATUSES`, counters, `expires_at` |
| Background execution with retries | `ReportedTask` in `core/tasks.py`, which logs a permanently-failed task once |
| Enqueue after commit | `transaction.on_commit(lambda: task.delay(...))` — the campaign module's own idiom |
| Tenant-scoped file keys | `utils.blob_storage.keys.build_tenant_object_key(tenant_id, folder, filename)` |
| Ownership check on a key | `is_object_key_owned_by_tenant` |
| Short-lived download links | The SAS signing behind `get_presigned_url_for_object` — **extended** as `get_presigned_download_url(key, file_name)`, which also signs a `Content-Disposition` so the file saves under its readable name |
| Writing a file from the server | **Nothing existed** — every upload path was a browser PUT. `AzureBlobStorageService.upload_file` was added |
| Telling a user something happened | `InAppNotification`, with a new `REPORTS` notification type |
| Beat sweeps | The existing `CELERY_BEAT_SCHEDULE` block |

**Almost nothing new is needed in storage, queuing or notification** — one server-side
upload method and one notification type. What is new is one model, three tasks, one
service with two writers, and one viewset plus one campaign action.

---

## 2. Design decisions

### 2.1 The row is created by the request, not the worker

`POST` writes a `CampaignReport` in `PENDING` and returns **202** with its id. The task
only ever *updates* a row that already exists.

The alternative — task creates the row — means the id cannot be returned to the caller,
so there is nothing to poll until the worker gets round to it. A queue backed up by
five minutes would look identical to a request that silently failed.

### 2.2 Enqueue on commit, never inline

```python
transaction.on_commit(lambda: generate_campaign_report.delay(str(report.pk)))
```

A task fired before its transaction commits races the row it is about to read, and on a
fast broker the worker wins. The campaign module already learned this; the same rule
applies here.

### 2.3 Stream to a temporary file, upload once

Write to `tempfile.NamedTemporaryFile` row by row, then upload the finished file.

- Building in memory means a 100k-row report sits entirely in a worker's RSS, and a few
  concurrent reports are how a worker gets OOM-killed.
- Incremental upload means a partially-written blob exists at a key the download
  endpoint could reach if the status were ever wrong.

Uploading last also gives the failure guarantee §9 of the feature doc asks for: **a job
that fails before the upload leaves nothing behind.**

### 2.4 XLSX via openpyxl's write-only mode

`openpyxl` is already a dependency, but its **default mode holds the whole workbook in
memory** — which is why an earlier draft of this plan assumed XLSX could not be streamed
at all. It can:

```python
wb = openpyxl.Workbook(write_only=True)
sheet = wb.create_sheet("Members")
for member in queryset.iterator(chunk_size=2000):
    sheet.append(row_for(member))
```

`write_only=True` serialises each row to a temp file as it is appended and never holds
the sheet. It is the difference between this feature being possible and not, so it is
not an optimisation to add later — **build it this way from the first commit**, because
retrofitting it means rewriting the writer.

The constraint it carries: rows can only be appended, never revisited. Column widths and
freeze panes must be set before the rows go in, and anything needing a total at the
bottom has to be computed first.

**As built, the overview is the first sheet, computed before the member pass** with SQL
aggregates (`build_overview`), not totals accumulated while streaming. An earlier draft
wrote the overview after the members, out of the same pass — which would have put it
second in the workbook, when the overview is what a reader opens a report to see.

Two hazards of free text surfaced in the writer and are handled there, once, for every
text cell:

- **Control characters.** openpyxl raises `IllegalCharacterError`, so one stray byte in a
  runner's outcome would fail the whole report. Stripped.
- **Formulas.** openpyxl turns any string starting with `=` into a formula, so a contact
  named `=HYPERLINK(…)` would be live in a client's Excel. The cell type is pinned to
  string.

### 2.5 PDF via ReportLab, not WeasyPrint

WeasyPrint renders nicer HTML/CSS, but it needs pango, cairo and gdk-pixbuf at the
system level. The Dockerfile is `python:3.13-slim` with a deliberately minimal apt layer
that purges `gcc` afterwards "to shrink attack surface" — adding three graphics stacks
to it works against a decision somebody made on purpose.

ReportLab is pure Python with a wheel, and its `platypus` `LongTable` does the two
things this report actually needs: **repeat a header row across page breaks**, and
switch page orientation mid-document. Styling a table is more code than CSS would be;
that is the trade, and it is the right one here.

### 2.6 One format per report

`report_format` is chosen at request time and produces one file. Two formats means two
requests and two rows.

The alternative — one job emitting both — gives one status covering two pieces of work
with very different costs and failure modes, and no way to report that the workbook
succeeded while the PDF ran out of memory.

### 2.7 Progress is written every N rows, not every row

`PROGRESS_UPDATE_EVERY = 500`. One `UPDATE` per row is 100,000 writes to move a number
nobody reads to that resolution; one per 500 is 200 writes and an equally useful bar.

### 2.8 Download returns a link, never proxies

`GET /download/` returns **200** with a freshly minted signed URL in the body. Streaming
through Django would occupy a gunicorn worker for the length of the transfer to achieve
nothing storage does not already do.

**Changed from the first draft, which returned a 302.** The API authenticates with a
bearer token, which a browser cannot attach to a plain link, so the frontend has to
`fetch` this endpoint — and a `fetch` that follows a 302 to storage pulls the file into
JavaScript memory, or is refused by the storage account's CORS rules, instead of saving
it. Returned in the body, the URL is something the client can navigate to, and the
browser downloads straight from storage.

The URL is minted per request and never stored. A stored URL is a credential with an
expiry nobody is tracking. It is signed with a `Content-Disposition`
(`get_presigned_download_url`), so the file saves under its readable name rather than
its blob key.

---

## 3. The model

`core/models/campaign_report.py`, extending `TenantBaseModel` (audited — knowing who
asked for a report is the point of half of them).

| Field | Type | Notes |
|---|---|---|
| `campaign` | FK → Campaign, `PROTECT` | A report is about a campaign; deleting one out from under its reports loses the context that makes them readable |
| `status` | choices | `PENDING`, `RUNNING`, `COMPLETED`, `FAILED`, `EXPIRED` |
| `report_format` | choices | `XLSX` (default) or `PDF`. One file per row |
| `file_key` | char, blank | The blob key. Blank until upload succeeds |
| `file_size_bytes` | big int | For the UI to show before someone downloads 40MB on mobile |
| `total_rows` | int | Known after the first count |
| `processed_rows` | int | Progress |
| `started_at`, `completed_at` | datetime, null | |
| `error_message` | text, blank | Populated on `FAILED`; the user-facing explanation |
| `expires_at` | datetime, indexed | Drives the retention sweep |

**Not a field:** `download_url`. It is derived per request, and storing it would mean
persisting a credential that silently expires.

```python
OPEN_STATUSES = [PENDING, RUNNING]
TERMINAL_STATUSES = [COMPLETED, FAILED, EXPIRED]
```

Indexes: `(tenant, campaign, -created_at)` for the list, `(status, expires_at)` for the
two sweeps.

---

## 4. File-by-file changes

| File | Change |
|---|---|
As built:

| File | Change |
|---|---|
| `core/models/campaign_report.py` | **New.** The model above |
| `core/models/__init__.py` | Export it |
| `core/models/notification_log.py` | New `REPORTS` notification type |
| `core/migrations/0060_…` | The model and its two indexes, plus the choices change on both notification models |
| `core/admin/campaign_report_admin.py` + `__init__.py` | **New.** Read-only admin: no add, every field read-only |
| `core/services/campaign_reports.py` | **New.** `request_report`, `build_report`, `member_queryset` / `member_rows`, `build_overview`, `mark_failed`, `download_payload`, `expire_reports`, `fail_stalled_reports` |
| `core/services/report_writers/__init__.py` | **New.** `ReportContent` and `Column` — what a report contains, independent of layout |
| `core/services/report_writers/xlsx.py` | **New.** Two-sheet workbook, write-only mode |
| `core/services/report_writers/pdf.py` | **New.** Portrait overview, landscape member table |
| `utils/blob_storage/service.py` | `upload_file` and `get_presigned_download_url`; `_sign` accepts a `content_disposition` |
| `pyproject.toml` / `uv.lock` | Add `reportlab` (5.0.1 locked). `openpyxl` was already there |
| `core/tasks.py` | `generate_campaign_report`, `expire_campaign_reports`, `fail_stalled_campaign_reports` |
| `core/serializers/campaign_report.py` | **New.** Detail, list, dropdown and ref tiers, plus the request body |
| `core/views/campaign_report.py` | **New.** Read-only viewset: list, retrieve, dropdown, `download` |
| `core/views/campaign.py` | `@action(detail=True, methods=["post"], url_path="reports")`, on the shared campaign-action throttle |
| `core/urls/campaign_report.py` + `__init__.py` | Route it |
| `config/settings/base.py` | Five `CAMPAIGN_REPORT_*` settings, two beat entries, a `CampaignReportStatusEnum` name override |
| `.env.example` | Documented settings |
| `core/tests/campaigns/test_campaign_report.py` | **New.** See §8 |
| `core/tests/test_upload_tickets.py` | The download SAS's signed `Content-Disposition` |
| `docs/apiDocs/core/campaign_report.md` | **New.** API reference |
| `docs/apiDocs/core/campaign.md`, `in_app_notification.md`, `docs/_sidebar.md`, `docs/campaigns/README.md` | The new action, the new notification type, and links |

### The service boundary

`request_report(campaign, user)` does the validation, writes the row and schedules the
task. The viewset calls it; so can a future scheduled-reports feature, without
duplicating the rules.

`build_report(report_id)` is the whole of the work, and takes an id rather than an
instance — a task argument must survive JSON serialisation.

---

## 5. The generation task

As built:

```python
@shared_task(base=ReportedTask, bind=True, max_retries=3, default_retry_delay=60)
def generate_campaign_report(self, report_id):
    from core.services import campaign_reports as report_service

    try:
        return report_service.build_report(report_id)
    except report_service.PermanentReportError as exc:
        report_service.mark_failed(report_id, str(exc))
        return None
    except Exception as exc:
        if self.request.retries >= self.max_retries:
            failure_logger.error("generate_campaign_report gave up on %s ...", report_id, exc_info=True)
            report_service.mark_failed(report_id, report_service.GENERIC_FAILURE_MESSAGE)
            return None
        raise self.retry(exc=exc, countdown=60 * 2 ** self.request.retries)
```

Same split the campaign handoff task uses: a permanent error (campaign gone) is recorded
and dropped; a transient one (storage hiccup) is retried after 1, 2 and 4 minutes, and
the last attempt records the failure **before** returning, or the row sits with nothing
to explain it.

**The user-facing message is generic; the exception goes to `core.tasks.failures`.** The
first draft wrote `str(exc)` into `error_message`, which would put a storage SDK's error
text in front of a tenant and into their notification.

### `build_report` sequence

1. **Claim** under `select_for_update`: `PENDING` → `RUNNING`, `started_at`, and
   `total_rows` from one `count()`. Anything not `PENDING` is skipped, so a re-delivered
   task cannot rebuild a report.
2. Load the campaign, **excluding soft-deleted** — a deleted campaign is a
   `PermanentReportError`.
3. Build the overview from SQL aggregates, then stream members into a temp file
   (`mkstemp`, removed in `finally`), writing progress every
   `CAMPAIGN_REPORT_PROGRESS_EVERY` rows.
4. Upload to `build_tenant_object_key(tenant, "campaign-reports", file_name,
   unique_id=report.pk)`. The name uses the report's **creation** time, so a retry lands
   on the same key and overwrites instead of orphaning.
5. `COMPLETED` with `file_key`, `file_size_bytes`, `rows_included`, `completed_at` and
   `expires_at` — as a conditional `UPDATE … WHERE status = RUNNING`. If that touches no
   row, the stall sweep failed the report while it was building, and the file just
   uploaded is deleted, since nothing refers to it.
6. Notify — in its own `try`, because a missing notification is not a failed report.

**On any exception in 2–5**, the row is handed back to `PENDING` with `processed_rows`
reset before the error propagates. A retry that found it still `RUNNING` would skip its
own report at step 1.

Steps 2–5 are **not** one transaction. Holding one open for the minutes a 100k report
takes would pin a connection and block nothing useful; each status write is its own
small commit, which is also what makes progress visible to a poller.

---

## 6. Memory and query shape

The part most likely to be got wrong.

```python
CampaignMember.objects.filter(campaign=campaign)
    .select_related("contact")
    .order_by("pk")
    .iterator(chunk_size=2000)
```

- **`.iterator()`**, so the full result set is never materialised. Without it, 100k
  members with their contacts is hundreds of MB in a worker.
- **`select_related("contact")`** — the name comes from the contact, and without it this
  is 100,000 extra queries.
- **`.order_by("pk")`** — `iterator()` over an unordered queryset can repeat or skip
  rows under concurrent writes. The campaign module's `resolve_audience` already carries
  this exact warning.

**Call data needs care.** `AgentCallLog` points *at* `CampaignMember`, not the reverse,
so there is no `select_related` for it, and prefetching call logs for 100k members
would defeat the streaming.

Annotate the member queryset instead — one `Subquery` per column, evaluated in the
same scan:

```python
calls = AgentCallLog.objects.filter(campaign_member=OuterRef("pk"))
.annotate(
    duration=Subquery(calls.values("duration_seconds")[:1]),
    credits=Subquery(calls.values("credits_charged")[:1]),
    call_id=Subquery(calls.values("provider_call_id")[:1]),
)
```

**`credits_charged` is why this works.** Without Phase 0 the credit figure is not on
any joinable row and would need a second chunked pass over the ledger, keyed by
provider call id and suffix-aware. With it, credits are an ordinary column on a table
already being joined.

`[:1]` because a member should have one call log, but nothing enforces it — a retried
runner could post two. Taking the first is a decision; letting the subquery raise on
the second is not a behaviour anyone wants mid-report.

---

## 7. Expiry and the stuck-job sweep

Two beat tasks, doing different jobs.

**`expire_campaign_reports`** — hourly. Finds `COMPLETED` reports past `expires_at`,
deletes the blob, sets `EXPIRED` and clears `file_key`. The row stays: "this report
existed and has been cleaned up" is a better answer than a 404. The blob is deleted
**first** — if that fails the row stays `COMPLETED` and the next run tries again, rather
than forgetting the file exists. Hourly rather than the daily first planned: the query is
cheap on the `(status, expires_at)` index, and a file outliving its window by a day is a
day of a client's data held for nobody.

**`fail_stalled_campaign_reports`** — every 15 minutes. Fails open reports that have made
**no progress** for `CAMPAIGN_REPORT_STALL_MINUTES` (120), through `mark_failed`, so the
requester is told. Two cases:

- `RUNNING` — a worker was killed mid-report.
- `PENDING` — the task was never delivered: a lost `on_commit`, a broker outage. The first
  draft covered only `RUNNING`, which left this case waiting forever.

The sweep exists because **nothing else will ever move them.** A user watching a progress
bar that will never advance has no way to learn that.

**Measured from `updated_at`, not `started_at` or `created_at`.** Every progress write,
claim and hand-back to `PENDING` touches it, so the window is time without progress.
Measured from the start, the sweep would fail a large build in the middle of writing it;
measured from creation, it would fail a report waiting out a retry's backoff just before
its last attempt. The window must still exceed the longest *silent* stretch of a
legitimate build — the overview aggregates, the final save, the upload. A build that
does finish after being failed discards its own upload (§5, step 5).

---

## 8. Test plan

As built: one module, `core.tests.campaigns.test_campaign_report`, rather than the
`core.tests.reports` package first planned — plus the SAS assertions in
`core.tests.test_upload_tickets`. Postgres, narrow labels.

**The row and the request**
- `POST /campaigns/{id}/reports/` returns 202 with a `PENDING` row and enqueues exactly one
  task (`captureOnCommitCallbacks`, or the `on_commit` hook never fires under `TestCase`).
- The requester is recorded; XLSX is the default; PDF can be requested; an unknown format
  is a 400.
- Another tenant's campaign is a **404**, not a 403, and queues nothing.
- Two requests make two independent rows.

**Generation — asserted on the file itself**, reopened with openpyxl from the bytes handed
to storage, not on which functions were called
- Happy path: `COMPLETED`, `file_key`, size and row count match; the sheets are in order.
- **Suppressed members appear in the file**, with their reason — the one thing this
  report does that the rest of the campaign module deliberately does not.
- Phone is the frozen `phone_e164`, not the contact's current number.
- Duration, credits and provider call id come from the call log; no call means empty
  cells, not zeros.
- Times are local to the campaign's zone, and the header names the zone.
- A contact named `=HYPERLINK(…)` comes back as a string cell; a control character does not
  fail the report.
- An empty campaign produces a header-only file, not a failure.
- The overview reconciles to the total, cancelled included, and sums credits.
- A non-`PENDING` report is skipped, so a redelivered task cannot rebuild it.
- PDF: a real PDF; capped at `CAMPAIGN_REPORT_PDF_MAX_ROWS` while the workbook is not;
  markup, control characters and 5,000 characters of unbroken text do not fail it.

**Failure**
- Upload failure hands the row back to `PENDING` with **no downloadable file**; a
  transient failure retries.
- Retries exhausted → `FAILED` with the generic message, logged to `core.tasks.failures`,
  and the requester notified.
- A deleted campaign fails at once, without a retry.
- Notification failure → report still `COMPLETED`.

**API**
- `download` on a `PENDING` report is a **409**.
- `download` on `COMPLETED` returns 200 with a freshly signed link; no link and no key is
  ever serialised on the report.
- Another tenant's report is a 404; the list is tenant-scoped and filterable.
- `PATCH` and `DELETE` are 405.

**Sweeps**
- Expiry deletes the blob, sets `EXPIRED` and keeps the row; a fresh report is untouched;
  a failed blob delete leaves the report `COMPLETED` for the next run.
- A report with no progress for the window is failed, `RUNNING` or `PENDING`, and the
  requester told; a long build still writing, and a report waiting out a retry, are not.

**Storage**
- The named download SAS is read-only and signs `Content-Disposition` with the file name,
  quotes dropped. The plain download SAS used by the ETL service signs none.

---

## 9. Phasing

Each phase is shippable on its own.

**Status: every phase is built** (13 September 2026) — built in one pass rather than
shipped phase by phase. Not yet committed or deployed; see [Deploying](#deploying).

**Phase 0 — credits on the call log.** ✅ Section 0. A prerequisite, not a nicety: the
report cannot show a credits column without it, and the join it replaces is wrong.
Shippable and useful on its own — it also makes the dashboard's credit figures a
field read instead of a three-table join.

**Phase 1 — the pipeline, XLSX only.** ✅ Model, migration, service, task, `POST` + status
endpoints, and the two-sheet workbook. This is the risky half: streaming 100k rows
without exhausting a worker is the thing that either works or does not, and everything
else is decoration on top of it.

**Phase 2 — delivery.** ✅ Download link (a 200, not the redirect first planned — §2.8),
`InAppNotification` on completion *and* on failure, list endpoint and filters. This is
what makes it usable by a person rather than a developer.

**Phase 2b — PDF.** ✅ The ReportLab writer, the row cap, and the format choice on the
request. Deliberately after delivery: the workbook is the format people will actually
filter and pivot, and the PDF is worth building once the pipeline underneath it is
proven rather than while it is still moving.

**Phase 3 — housekeeping.** ✅ Both sweeps, retention settings, API docs. Nothing here is
urgent on day one, and all of it is needed by month one — an unswept `RUNNING` row and
an unbounded storage bill both take weeks to become visible.

### Deploying

1. **Rebuild the image.** `reportlab` is a new dependency and brings `pillow` with it;
   `uv.lock` is updated.
2. **Migrate.** `0059` adds `AgentCallLog.credits_charged`; `0060` creates
   `CampaignReport` and adds the `REPORTS` notification type. Both are additive.
3. **Restart the workers and beat.** Workers must load `generate_campaign_report`, or a
   requested report waits in `PENDING` until they do — and after two hours the stall
   sweep fails it. Beat must load the two new schedule entries.
4. **Backfill credits** on existing call logs: `manage.py backfill_call_credits
   --dry-run`, then without the flag. Until then, a report on an older campaign shows
   blank credits for calls charged before Phase 0.
5. **Storage needs nothing.** Reports go to the existing container, under a
   `campaign-reports/` folder inside each tenant's prefix.

---

## 10. Out of scope

- **CSV.** The workbook is strictly better for the same reader, and a third format is a
  third writer to keep consistent with the other two.
- **Scheduled or recurring reports.** `request_report` is already the seam; the
  scheduling is a separate feature.
- **Cross-campaign reports.** One campaign per report. Rolling up several belongs to the
  analytics module.
- **Emailing reports.** Needs answers about link lifetime and attachment size that
  nobody has asked for yet.
- **Column selection or templates.** One fixed layout until there is a second opinion
  about the columns.
- **Cancelling a running report.** Celery revocation is unreliable mid-task, and a
  report takes minutes. Let it finish and ignore it.
