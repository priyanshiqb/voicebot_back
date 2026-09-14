# Campaign Reports API

**Base paths:** `/api/v1/core/campaigns/{id}/reports/` (request) ·
`/api/v1/core/campaign-reports/` (everything else)
**Auth:** JWT — `Authorization: Bearer <access_token>`
**Scope:** every endpoint is filtered to the caller's tenant.

> **VB_AGENT access: none.** Every endpoint on this page returns **403** to an agent
> token. Reports are for people; the runner has no use for them.

A campaign report is an **XLSX workbook** or a **PDF** describing one campaign: an
overview of the campaign as a whole, then one row per member. It is built in the
background — the request returns immediately, and the file is ready seconds or minutes
later. For what the file contains and why it works this way, see
[Campaign Reports — Feature Detail](../../feature-detail-scratch/campaign-report.md).

---

## Contents

- [The flow](#the-flow)
- [Response envelope](#response-envelope)
- [Endpoint index](#endpoint-index)
- [Field reference](#field-reference)
- [Enum reference](#enum-reference)
- [`POST /campaigns/{id}/reports/` — Request](#post-campaignsidreports--request)
- [`GET /campaign-reports/` — List](#get-campaign-reports--list)
- [`GET /campaign-reports/{id}/` — Retrieve](#get-campaign-reportsid--retrieve)
- [`GET /campaign-reports/{id}/download/`](#get-campaign-reportsiddownload)
- [`GET /campaign-reports/dropdown/`](#get-campaign-reportsdropdown)
- [What is not available](#what-is-not-available)
- [Error reference](#error-reference)
- [Configuration](#configuration)
- [Related](#related)

---

## The flow

1. **Request.** `POST /campaigns/{id}/reports/` → **202** with a report in `PENDING`.
2. **Poll.** `GET /campaign-reports/{id}/` every 3–5 seconds, showing `progress_percent`,
   until `status` is `COMPLETED`, `FAILED` or `EXPIRED`.
3. **Download.** When `is_downloadable` is `true`, call
   `GET /campaign-reports/{id}/download/` and send the browser to `data.download_url`.

```js
// 1. request
const { data: { data: report } } = await api.post(
  `/api/v1/core/campaigns/${campaignId}/reports/`, { report_format: "XLSX" }
);

// 2. poll until terminal
let current = report;
while (["PENDING", "RUNNING"].includes(current.status)) {
  await new Promise((r) => setTimeout(r, 4000));
  current = (await api.get(`/api/v1/core/campaign-reports/${report.id}/`)).data.data;
}

// 3. download
if (current.is_downloadable) {
  const link = (await api.get(`/api/v1/core/campaign-reports/${report.id}/download/`)).data.data;
  window.location.assign(link.download_url);
}
```

The requester also gets an **in-app notification** (`notification_type: "REPORTS"`) when
the report is ready or has failed, so a user who navigated away still finds out. Polling
is for the screen they are on; the notification is for everywhere else.

---

## Response envelope

The same wrapper as every other endpoint — see the
[Campaigns API](campaign.md#response-envelope). In short:

```json
{ "status": true, "message": "success", "data": {}, "errors": null }
```

Errors put `status: false`, a displayable `message`, and field errors under `errors`, and
carry a `request_id`. Lists add `meta` with `count`, `next`, `previous`, `page` and
`page_size`.

---

## Endpoint index

| Method | Path | Purpose | Success |
|---|---|---|---|
| `POST` | `/campaigns/{id}/reports/` | Request a report | 202 |
| `GET` | `/campaign-reports/` | List reports | 200 |
| `GET` | `/campaign-reports/{id}/` | One report — the poll target | 200 |
| `GET` | `/campaign-reports/{id}/download/` | A fresh download link | 200 |
| `GET` | `/campaign-reports/dropdown/` | Picker options | 200 |

---

## Field reference

A report has **no writable fields**. The only input is the request body of
[`POST /campaigns/{id}/reports/`](#post-campaignsidreports--request); everything below is
set by the server.

| Field | Type | Description |
|---|---|---|
| `id` | UUID | The report. |
| `campaign` | object | `{ "id", "name", "status" }` of the campaign reported on. |
| `report_format` | enum | `XLSX` or `PDF` — see [Enum reference](#enum-reference). |
| `status` | enum | Where the report is — see [Enum reference](#enum-reference). |
| `progress_percent` | integer | `0`–`99` while the report is open; `100` once it is `COMPLETED`, and still `100` after it has `EXPIRED`. It stops short of 100 while open because the last row is followed by an upload. On a `FAILED` report it is whatever was reached — ignore it. |
| `processed_rows` | integer | Members written so far. Updated every 500 rows, so it moves in steps. |
| `total_rows` | integer | Members in the campaign when generation started. `0` until a worker picks the report up. |
| `rows_included` | integer | Members in the finished file. Below `total_rows` only for a PDF past its row cap. |
| `file_name` | string | What the download saves as, e.g. `campaign-q1-lead-qualification-20270215-093012.xlsx`. `""` until `COMPLETED`. |
| `file_size_bytes` | integer | Size of the file, for showing before download. `0` until `COMPLETED`. |
| `is_downloadable` | boolean | `true` only when `COMPLETED` and the file exists. **Gate the download button on this**, not on `status`. |
| `started_at` | datetime \| null | When a worker last claimed the report. |
| `completed_at` | datetime \| null | When the report completed **or failed**. |
| `expires_at` | datetime \| null | When the file will be removed. Set on completion. |
| `error_message` | string | Why a `FAILED` report failed, in words a user can act on. `""` otherwise. |
| `created_by` | object | **Who requested the report.** |
| `created_at`, `updated_at`, `deleted_at`, `is_deleted`, `is_active`, `updated_by`, `deleted_by`, `tenant` | — | The audit and tenant fields every tenant resource carries. |

The **list** returns a lighter shape: `processed_rows`, `total_rows`, `rows_included`,
`started_at` and `error_message` are omitted. Retrieve one report for those.

**There is never a download URL on a report**, in the list or in detail. A link is a
credential; it is minted by [`download`](#get-campaign-reportsiddownload) each time and
never stored, so none can leak through a cached list response.

---

## Enum reference

### `status`

| Value | Meaning | What a client should do |
|---|---|---|
| `PENDING` | Queued — or an attempt failed and a retry is waiting | Keep polling |
| `RUNNING` | Being built | Keep polling; show `progress_percent` |
| `COMPLETED` | The file is ready | Offer the download |
| `FAILED` | Gave up; `error_message` says why | Show the message and offer to request again |
| `EXPIRED` | The file was removed after the retention window | Offer to request again |

`PENDING` and `RUNNING` are open; the other three are terminal and never change.

> **Progress can go backwards.** If an attempt fails part-way — storage unavailable, for
> instance — the report returns to `PENDING` with `processed_rows` reset to `0`, and the
> retry starts again from the first row. Render whatever the latest poll says.

### `report_format`

| Value | File | Contents |
|---|---|---|
| `XLSX` | Excel workbook | Sheet **Campaign Overview**, then sheet **Members** with every member, a frozen header and an autofilter. |
| `PDF` | PDF document | The overview on portrait pages, then a landscape member table with its header repeated on every page — capped at the first **2,000** members, and it says so when it is. |

---

## `POST /campaigns/{id}/reports/` — Request

Queue a report on one campaign. Returns immediately; the file is built in the background.

### Request

```json
{ "report_format": "PDF" }
```

| Field | Type | Description | Validation |
|---|---|---|---|
| `report_format` | enum | Which file to produce. | Optional, default `XLSX`. `XLSX` or `PDF` only. One format per request — for both, send two requests. |

An empty body `{}` requests an XLSX.

### Response `202`

```json
{
  "status": true,
  "message": "Report requested",
  "data": {
    "id": "7b1d…",
    "created_at": "2027-02-15T09:30:12Z",
    "updated_at": "2027-02-15T09:30:12Z",
    "deleted_at": null,
    "is_deleted": false,
    "created_by": { "id": "…", "email": "alex@acme.com", "first_name": "Alex", "last_name": "P" },
    "updated_by": { "id": "…", "email": "alex@acme.com", "first_name": "Alex", "last_name": "P" },
    "deleted_by": null,
    "tenant": { "id": "…", "name": "Acme", "subdomain": "acme" },
    "campaign": { "id": "3f2a…", "name": "Q1 Lead Qualification", "status": "RUNNING" },
    "report_format": "PDF",
    "status": "PENDING",
    "progress_percent": 0,
    "processed_rows": 0,
    "total_rows": 0,
    "rows_included": 0,
    "file_name": "",
    "file_size_bytes": 0,
    "is_downloadable": false,
    "started_at": null,
    "completed_at": null,
    "expires_at": null,
    "error_message": "",
    "is_active": true
  },
  "errors": null
}
```

### Responses

| Code | When | `message` | `errors` |
|---|---|---|---|
| **202** | Queued. Poll `data.id`. | `Report requested` | `null` |
| **400** | Unknown `report_format`. | `"CSV" is not a valid choice.` | `{"report_format": [...]}` |
| **401** | Missing or invalid token. | `Authentication credentials were not provided.` | `{"detail": …}` |
| **403** | No active tenant mapping, or a `VB_AGENT` token. | `Your user account is not associated with any active tenant.` / `This endpoint is not available to agent tokens.` | `{"detail": …}` |
| **404** | No such campaign, soft-deleted, **or another tenant's**. | `Not found.` | `{"detail": …}` |
| **429** | Throttled. Shares one 30/min budget with `launch`, `cancel` and `preview-audience`. | `Request was throttled. Expected available in N seconds.` | `{"detail": …}` |

> **Every request makes a new report.** Two clicks are two reports, each built in full.
> Disable the button while the request is in flight.

> **Any campaign status is allowed**, including `DRAFT`. A draft's report is mostly
> empty, which is itself a true answer.

---

## `GET /campaign-reports/` — List

This tenant's reports, newest first.

### Query parameters

| Parameter | Type | Description | Validation |
|---|---|---|---|
| `campaign` | UUID | Only this campaign's reports. | Must be a campaign id; an unknown one is **400**. |
| `status` | enum | Only reports in this status. | One of the [`status` values](#status). |
| `report_format` | enum | Only this format. | `XLSX` or `PDF`. |
| `ordering` | string | Sort order. | `created_at`, `-created_at` (default), `completed_at`, `-completed_at`. |
| `page` | integer | Page number. | Default `1`. |
| `page_size` | integer | Rows per page. | Default `10`, at most `100`. |

`?campaign=<id>` is what a campaign detail screen wants: "reports for this campaign".

### Response `200`

```json
{
  "status": true,
  "message": "success",
  "data": [
    {
      "id": "7b1d…",
      "created_at": "2027-02-15T09:30:12Z",
      "updated_at": "2027-02-15T09:30:41Z",
      "deleted_at": null,
      "is_deleted": false,
      "created_by": { "id": "…", "email": "alex@acme.com", "first_name": "Alex", "last_name": "P" },
      "updated_by": { "id": "…", "email": "alex@acme.com", "first_name": "Alex", "last_name": "P" },
      "deleted_by": null,
      "tenant": { "id": "…", "name": "Acme", "subdomain": "acme" },
      "campaign": { "id": "3f2a…", "name": "Q1 Lead Qualification", "status": "RUNNING" },
      "report_format": "XLSX",
      "status": "COMPLETED",
      "progress_percent": 100,
      "file_name": "campaign-q1-lead-qualification-20270215-093012.xlsx",
      "file_size_bytes": 1843201,
      "is_downloadable": true,
      "completed_at": "2027-02-15T09:30:41Z",
      "expires_at": "2027-02-22T09:30:41Z",
      "is_active": true
    }
  ],
  "errors": null,
  "meta": { "count": 1, "next": null, "previous": null, "page": 1, "page_size": 10 }
}
```

| Code | When |
|---|---|
| **200** | Listed. An empty `data` is not an error. |
| **400** | A filter value is invalid. |
| **401** / **403** | As elsewhere. |

---

## `GET /campaign-reports/{id}/` — Retrieve

One report, with progress and error detail. **This is the poll target.** It reads one row
and is cheap to call every few seconds.

### Response `200` — while running

```json
{
  "status": true,
  "message": "Retrieved successfully",
  "data": {
    "id": "7b1d…",
    "campaign": { "id": "3f2a…", "name": "Q1 Lead Qualification", "status": "COMPLETED" },
    "report_format": "XLSX",
    "status": "RUNNING",
    "progress_percent": 42,
    "processed_rows": 21000,
    "total_rows": 50000,
    "rows_included": 0,
    "file_name": "",
    "file_size_bytes": 0,
    "is_downloadable": false,
    "started_at": "2027-02-15T09:30:14Z",
    "completed_at": null,
    "expires_at": null,
    "error_message": "",
    "…": "audit and tenant fields as in the 202 example"
  },
  "errors": null
}
```

### Response `200` — failed

```json
{
  "status": true,
  "message": "Retrieved successfully",
  "data": {
    "id": "7b1d…",
    "status": "FAILED",
    "progress_percent": 0,
    "is_downloadable": false,
    "completed_at": "2027-02-15T09:38:02Z",
    "error_message": "The report could not be generated. Please request it again.",
    "…": "remaining fields as above"
  },
  "errors": null
}
```

The `error_message` values a client can meet:

| `error_message` | Cause |
|---|---|
| `The report could not be generated. Please request it again.` | Every retry failed — storage or the database was unavailable. The detail is in the server logs. |
| `Report generation stopped before it finished. Please request it again.` | The report made no progress for two hours — its worker died, or its task never arrived. |
| `The campaign was deleted before its report could be generated.` | The campaign was soft-deleted between the request and the build. |

| Code | When | `message` |
|---|---|---|
| **200** | Found. | `Retrieved successfully` |
| **401** / **403** | As elsewhere. | — |
| **404** | No such report, **or another tenant's**. | `Not found.` |

---

## `GET /campaign-reports/{id}/download/`

A fresh, short-lived link to the file.

### Response `200`

```json
{
  "status": true,
  "message": "Download link issued",
  "data": {
    "download_url": "https://acmestorage.blob.core.windows.net/media/tenants/…/campaign-reports/7b1d…_campaign-q1-lead-qualification-20270215-093012.xlsx?sv=…&se=…&sp=r&rscd=…&sig=…",
    "file_name": "campaign-q1-lead-qualification-20270215-093012.xlsx",
    "file_size_bytes": 1843201,
    "expires_in_seconds": 3600
  },
  "errors": null
}
```

| Field | Meaning |
|---|---|
| `download_url` | A signed, read-only link straight to storage. Send the browser to it; no `Authorization` header is needed or wanted. |
| `file_name` | What the file saves as — the link is signed to download under this name. |
| `file_size_bytes` | Size, for a "1.8 MB" label. |
| `expires_in_seconds` | How long the link works. After that, call this endpoint again. |

### Responses

| Code | When | `message` |
|---|---|---|
| **200** | Link issued. | `Download link issued` |
| **401** / **403** | As elsewhere. | — |
| **404** | No such report, or another tenant's. | `Not found.` |
| **409** | `PENDING` | `This report is queued and not ready yet.` |
| **409** | `RUNNING` | `This report is still being generated.` |
| **409** | `FAILED` | `This report failed and has no file. Request a new one.` |
| **409** | `EXPIRED` | `This report has expired. Request a new one.` |

> **Why JSON and not a redirect.** This endpoint needs a bearer token, which a plain link
> cannot carry, so it has to be called with `fetch`. A `fetch` that followed a redirect to
> storage would pull the whole file into JavaScript memory — or be refused by the storage
> account's CORS rules — instead of saving it. Returning the link lets the browser
> download straight from storage.

> **Never store `download_url`.** Call this endpoint each time the user clicks download.
> A saved link stops working after an hour, and it is a credential: anyone holding it can
> fetch the file until then.

---

## `GET /campaign-reports/dropdown/`

Picker options. Takes the same filters as the list, plus `?limit=` (at most `1000`).

```json
{
  "status": true,
  "message": "success",
  "data": [
    { "label": "campaign-q1-lead-qualification-20270215-093012.xlsx", "value": "7b1d…" },
    { "label": "PDF document (Queued)", "value": "8c2e…" }
  ],
  "errors": null,
  "meta": { "count": 2, "next": null, "previous": null, "page": 1, "page_size": 10 }
}
```

`label` is the file name once there is one, and the format with its status before that.

---

## What is not available

| Request | Result | Why |
|---|---|---|
| `POST /campaign-reports/` | **405** | Reports are requested on their campaign: `POST /campaigns/{id}/reports/`. |
| `PUT` / `PATCH /campaign-reports/{id}/` | **405** | A report is changed only by the job that builds it. |
| `DELETE /campaign-reports/{id}/` | **405** | Removing a report would orphan its file. Files are removed by the retention sweep, which deletes the file first and keeps the row as `EXPIRED`. |
| Cancelling a running report | — | Not supported. A report takes minutes; let it finish. |

---

## Error reference

**Every error uses the standard envelope** and carries `request_id`.

| Code | Meaning |
|---|---|
| `400` | Unknown `report_format`, or an invalid list filter. |
| `401` | Missing, malformed, or expired token. |
| `403` | No active tenant mapping, or a `VB_AGENT` token. |
| `404` | Not found, soft-deleted, **or another tenant's**. Never 403, which would confirm it exists. |
| `405` | A method this resource does not offer — see [What is not available](#what-is-not-available). |
| `409` | `download` on a report with no file. The `message` names the status. |
| `429` | Report requests are throttled with the campaign actions: 30/min per user, shared. |

---

## Configuration

| Setting | Default | Effect |
|---|---|---|
| `CAMPAIGN_REPORT_RETENTION_DAYS` | `7` | Days a file stays downloadable. Sets `expires_at`. |
| `CAMPAIGN_REPORT_PROGRESS_EVERY` | `500` | Rows between `processed_rows` updates. |
| `CAMPAIGN_REPORT_PDF_MAX_ROWS` | `2000` | Most members a PDF lists. The workbook always has everyone. |
| `CAMPAIGN_REPORT_STALL_MINUTES` | `120` | How long a `RUNNING` or `PENDING` report may go without progress before it is failed. |
| `CAMPAIGN_REPORT_DOWNLOAD_URL_HOURS` | `1` | Lifetime of a download link — `expires_in_seconds`. |

The retention sweep runs **hourly**, so a file can outlive `expires_at` by up to an hour.
The stall sweep runs **every 15 minutes**. A failed attempt is retried three times, after
1, 2 and 4 minutes.

---

## Related

- [Campaign Reports — Overview & Complete Flow](../../campaigns/reports.md) — lifecycle, background jobs, failure modes, frontend guide
- [Campaigns API](campaign.md) — the campaign a report describes
- [Campaign Reports — Feature Detail](../../feature-detail-scratch/campaign-report.md) — every column, both layouts, and the design reasoning
- [In-App Notifications API](in_app_notification.md) — where the "report ready" notification appears
- [Campaign Overview](../../campaigns/README.md) — how campaigns run
