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
| Migration + index if the dashboard aggregates on it | `core/migrations/` |
| `consume()` returns the rated total alongside the primary entry | `wallet/services/wallet.py` |
| `_charge_credits` stores it on the log after a successful charge | `core/views/agent_call_log.py` |
| Backfill for existing rows | `wallet/management/commands/backfill_call_credits.py` |

**Null, not zero, as the default.** "Never charged" and "charged zero" are different
facts — the first is an unrated call the sweep should be reporting, the second is a
legitimate free call. A default of `0` merges them and the report can no longer tell a
reader which happened.

### Changing what `consume()` returns

It currently returns `entries[0]`, whose `.amount` is only the **first bucket's** share —
so the caller cannot learn the total from it. That is the whole reason this field cannot
simply be read off the return value today.

Return a small result object carrying the primary entry, the full entry list and
`result.credits`. The blast radius is small: **two production callers**, and neither
reads the return beyond checking it is not `None`.

| Caller | Uses the return for |
|---|---|
| `core/views/agent_call_log.py` `_charge_credits` | Nothing today; will store `credits_charged` |
| `wallet/admin/wallet.py` `charge_unrated_calls` | `is None` → "already charged / nothing owed" |

`wallet/tests/test_wallet_service.py` binds `entry = wallet_service.consume(...)` in
about a dozen places and does read `.amount`; those move to `result.entry.amount`. Ours
to update, and the compiler-less equivalent of a rename.

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

- A charge spanning both buckets records the **full** total, not the first bucket's
  share. *This is the regression test for the bug; it should have existed already.*
- A single-bucket charge records its amount.
- An uncharged call keeps `credits_charged` null.
- `consume()`'s new return shape carries the total, and both production callers still
  behave.
- Backfill fills historical rows, sums both keys, and leaves unrated calls null.

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
| Short-lived download links | `AzureBlobStorageService.get_presigned_url_for_object(key, expiry_hours=1)` |
| Telling a user something happened | `InAppNotification` |
| Beat sweeps | The existing `CELERY_BEAT_SCHEDULE` block |

**Nothing new is needed in storage, queuing or notification.** What is new is one model,
one task, one service and one viewset.

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
bottom has to be computed first. The overview sheet is written *after* the member sheet
for that reason — its aggregates come out of the same pass.

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

### 2.8 Download redirects, never proxies

`GET /download/` returns a **302** to a freshly minted presigned URL. Streaming through
Django would occupy a gunicorn worker for the length of the transfer to achieve nothing
storage does not already do.

The URL is minted per request and never stored. A stored URL is a credential with an
expiry nobody is tracking.

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
| `core/models/campaign_report.py` | **New.** The model above |
| `core/models/__init__.py` | Export it |
| `core/migrations/` | One migration: model + two indexes |
| `core/services/campaign_reports.py` | **New.** `request_report`, `build_report`, `rows_for`, `overview_for`, `expire_stale` |
| `core/services/report_writers/xlsx.py` | **New.** Two-sheet workbook, write-only mode |
| `core/services/report_writers/pdf.py` | **New.** Portrait overview page, landscape member table |
| `pyproject.toml` | Add `reportlab`. `openpyxl` is already there |
| `core/tasks.py` | `generate_campaign_report`, `expire_campaign_reports`, `fail_stalled_reports` |
| `core/serializers/campaign_report.py` | **New.** 4-tier per `AGENTS.md` |
| `core/views/campaign_report.py` | **New.** The viewset |
| `core/views/campaign.py` | One `@action(detail=True, methods=["post"], url_path="reports")` |
| `core/urls/campaign_report.py` + `__init__.py` | Route it |
| `config/settings/base.py` | Retention, progress interval, stall timeout, `CAMPAIGN_REPORT_PDF_MAX_ROWS`, two beat entries |
| `.env.example` | Documented settings |
| `docs/apiDocs/core/campaign_report.md` | **New.** API reference |
| `docs/_sidebar.md` | Link it under Campaigns |

### The service boundary

`request_report(campaign, user)` does the validation, writes the row and schedules the
task. The viewset calls it; so can a future scheduled-reports feature, without
duplicating the rules.

`build_report(report_id)` is the whole of the work, and takes an id rather than an
instance — a task argument must survive JSON serialisation.

---

## 5. The generation task

```python
@shared_task(base=ReportedTask, bind=True, max_retries=3, default_retry_delay=60)
def generate_campaign_report(self, report_id):
    from core.services import campaign_reports as service
    try:
        return service.build_report(report_id)
    except service.PermanentReportError as exc:
        service.mark_failed(report_id, str(exc))
        return None
    except Exception as exc:
        if self.request.retries >= self.max_retries:
            service.mark_failed(report_id, f"Gave up after {self.max_retries}: {exc}")
            return None
        raise self.retry(exc=exc)
```

Same split the campaign handoff task uses: a permanent error (campaign gone, no
permission) is recorded and dropped; a transient one (storage hiccup) is retried, and
the last attempt records the failure **before** Celery raises, or the row sits in
`RUNNING` with nothing to explain it.

### `build_report` sequence

1. Load the report; **return early unless `PENDING`** — a re-delivered task must not
   rebuild a finished report.
2. `RUNNING`, `started_at`, `total_rows` from one `count()`.
3. Open a temp file, write the header, stream rows, update progress every 500.
4. Upload to the tenant-scoped key.
5. `COMPLETED`, `file_key`, `file_size_bytes`, `completed_at`, `expires_at`.
6. Notify — in its own `try`, because a missing notification is not a failed report.

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

**`expire_campaign_reports`** — daily. Finds `COMPLETED` reports past `expires_at`,
deletes the blob, sets `EXPIRED`. The row stays: "this report existed and has been
cleaned up" is a better answer than a 404.

**`fail_stalled_reports`** — hourly. Finds `RUNNING` reports whose `started_at` is older
than the stall timeout and marks them `FAILED`.

The second exists because **nothing else will ever move them.** A worker killed
mid-report leaves a row claiming to be running forever, and a user watching a progress
bar that will never advance has no way to learn that. The timeout must comfortably
exceed the slowest legitimate report — start at 2 hours.

---

## 8. Test plan

Postgres, narrow labels: `core.tests.reports`.

**The row and the request**
- `POST` returns 202 with a `PENDING` row and enqueues exactly one task
  (`captureOnCommitCallbacks`, or the `on_commit` hook never fires under `TestCase`).
- Another tenant's campaign is a **404**, not a 403.
- Two requests make two independent rows.

**Generation**
- Happy path: `COMPLETED`, `file_key` set, row count matches the audience.
- **Suppressed members appear in the file** — the one thing this report does that the
  rest of the campaign module deliberately does not.
- Phone is the frozen `phone_e164`, not the contact's current number. Change the contact
  after materialisation and assert the report still shows what was dialled.
- Empty campaign produces a header-only file, not a failure.
- A non-`PENDING` report is skipped, so a redelivered task cannot rebuild it.

**Failure**
- Upload failure → `FAILED`, `error_message` set, **and no downloadable file left**.
- Retries exhausted → recorded before Celery gives up.
- Notification failure → report still `COMPLETED`.

**API**
- `download` on a `PENDING` report is a **409**, not a broken redirect.
- `download` on `COMPLETED` returns a 302 whose URL is not stored on the row.
- List is tenant-scoped and filterable by campaign and status.

**Sweeps**
- Expiry deletes the blob and sets `EXPIRED`; a still-fresh report is untouched.
- A `RUNNING` report older than the timeout becomes `FAILED`; a young one does not.

---

## 9. Phasing

Each phase is shippable on its own.

**Phase 0 — credits on the call log.** Section 0. A prerequisite, not a nicety: the
report cannot show a credits column without it, and the join it replaces is wrong.
Shippable and useful on its own — it also makes the dashboard's credit figures a
field read instead of a three-table join.

**Phase 1 — the pipeline, XLSX only.** Model, migration, service, task, `POST` + status
endpoints, and the two-sheet workbook. No PDF, no notification, no download endpoint —
verify by reading `file_key` and fetching the blob directly. This is the risky half:
streaming 100k rows without exhausting a worker is the thing that either works or does
not, and everything else is decoration on top of it.

**Phase 2 — delivery.** Download redirect, `InAppNotification`, list endpoint and
filters. This is what makes it usable by a person rather than a developer.

**Phase 2b — PDF.** The ReportLab writer, the row cap, and the format choice on the
request. Deliberately after delivery: the workbook is the format people will actually
filter and pivot, and the PDF is worth building once the pipeline underneath it is
proven rather than while it is still moving.

**Phase 3 — housekeeping.** Both sweeps, retention settings, API docs. Nothing here is
urgent on day one, and all of it is needed by month one — an unswept `RUNNING` row and
an unbounded storage bill both take weeks to become visible.

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
