# Campaign Runner API

**Base path:** `/api/v1/core/campaign-runner/`
**Auth:** agent token only — `Authorization: Bearer vb_<prefix>_<secret>`

Machine-facing handoff between this backend and the external voice runner. The runner pulls a
campaign's context, builds its own call queue, dials, and reports per-member outcomes back.

Human JWT users receive **403** on every endpoint here.

For architecture and flow diagrams, see the [Campaign Overview](../../campaigns/README.md). For
the human-facing side, see the [Campaigns API](campaign.md).

---

## Contents

- [Division of responsibility](#division-of-responsibility)
- [How the runner learns a campaign exists](#how-the-runner-learns-a-campaign-exists)
- [Authentication](#authentication)
- [Scoping warning](#-scoping-warning)
- [Endpoint index](#endpoint-index)
- [`GET /{id}/context/`](#get-idcontext)
- [`POST /{id}/members/update/`](#post-idmembersupdate)
- [`POST /{id}/complete/`](#post-idcomplete)
- [Retry semantics](#retry-semantics)
- [Integration walkthrough](#integration-walkthrough)
- [Error reference](#error-reference)
- [Known gaps](#known-gaps)
- [Related](#related)

---

## Division of responsibility

| This backend owns | The runner owns |
|---|---|
| Campaign definition, schedule, audience | **The call queue** |
| Suppression (DNC / consent) and its audit | Dialling, concurrency, pacing |
| The frozen record of what was handed over | Retry policy on busy / no-answer |
| Applying the updates that come back | Sending those updates |

The runner **never writes `CampaignMember` directly.** It posts outcomes and this backend applies
them, so status validation and counter maintenance stay in one place.

**Suppressed members are never sent.** Contacts on do-not-call, or with denied or withdrawn
consent, are filtered out before the context response is built. The runner does not have to
implement DNC and cannot get it wrong.

---

## How the runner learns a campaign exists

**This backend POSTs to the runner when a campaign is ready.** There is no list endpoint here and
no due-work queue — every route below is `detail=True`, keyed by a campaign id — so without that
notification the runner has no way to discover a campaign at all. The push is the discovery
mechanism, not a latency optimisation.

### What we send

`POST` to `CAMPAIGN_RUNNER_NOTIFY_URL`, with `X-API-Key: <CAMPAIGN_RUNNER_TOKEN>` and
`X-Handoff-Id` duplicating the body field so the runner can dedupe at its edge without parsing
JSON.

```json
{
  "campaign_id": "5fa85f64-…",
  "tenant_id": "3c1b9a02-…",
  "handoff_id": "9f1c7e44-…",
  "agent_id": "7b2d5a10-…",
  "pending_count": 470
}
```

| Field | Meaning |
|---|---|
| `campaign_id` | What to fetch from [`GET /{id}/context/`](#get-idcontext). |
| `tenant_id` | What to send back as `X-Tenant-ID` on every call. |
| `handoff_id` | **Idempotency key.** See the contract below. |
| `agent_id` | Lets the runner warm bot config before pulling context. |
| `pending_count` | Advisory, like `credits` — lets the runner size its queue. The authoritative list is what `/context/` returns. |

> ### The contract we ask of the runner
>
> **Two notifications carrying the same `handoff_id` are the same job — ignore the second.**
> **A different `handoff_id` for the same `campaign_id` supersedes the earlier one.**
>
> `handoff_id` is re-minted on every `/launch/`, so it identifies *this attempt* at running *this
> campaign*. Delivery retries repeat it; a relaunch replaces it. Without this rule, a retried or
> swept notification dials the audience twice.

### What we expect back

| Response | We do |
|---|---|
| `2xx` | Mark the handoff `SENT` and move the campaign to `RUNNING`. |
| `429` | Retry with backoff, honouring `Retry-After` when it is a plain number of seconds. |
| `5xx`, timeout, connection error | Retry with backoff — 30s, 60s, 120s, 240s, 480s. |
| Any other `4xx` | **Give up immediately.** A rejected token or malformed body fails identically on every attempt. |

A permanently failed handoff sets `handoff_status = FAILED` and leaves the campaign in
`HANDING_OFF` rather than failing it — the audience is already materialised and correct, and
`HANDING_OFF` is workable, so a manual replay can still pull `/context/`. A sweeper re-notifies
campaigns stuck there, reusing the same `handoff_id`.

### `handoff_status`

| Value | Set when |
|---|---|
| `PENDING` | Campaign materialised; nothing sent yet. |
| `SENT` | The runner accepted the notification. |
| `ACKED` | The runner pulled `/context/` for the first time. |
| `FAILED` | Delivery gave up. Visible, and retried by the sweeper. |

> **It only ever moves forward.** The runner can pull `/context/` and reach `ACKED` before the
> notify task has committed `SENT`, so the writes are guarded — an acknowledged campaign is never
> dragged back. Do not infer ordering from it beyond "at least this far".

### Configuration

| Setting | Default | Purpose |
|---|---|---|
| `CAMPAIGN_RUNNER_NOTIFY_ENABLED` | `false` | Master switch. Off, materialisation goes straight to `RUNNING` and nothing is sent. |
| `CAMPAIGN_RUNNER_NOTIFY_URL` | *(empty)* | The runner's campaign-start endpoint. No default on purpose. |
| `CAMPAIGN_RUNNER_TOKEN` | *(empty)* | API key for the push, sent as the `X-API-Key` header. **Distinct from the `VB_AGENT` token the runner uses to call back** — this one authenticates us *to* the runner. |
| `CAMPAIGN_RUNNER_TIMEOUT` | `10` | Seconds. |
| `CAMPAIGN_RUNNER_STALE_HANDOFF_MINUTES` | `20` | How long a campaign may sit in `HANDING_OFF` before the sweeper re-notifies. Must exceed the retry budget above. |

---

## Authentication

```http
Authorization: Bearer vb_a1b2c3_s3cr3tstring
```

Tokens are issued to `VB_AGENT` users. `AgentTokenAuthentication` is a default auth class, so no
special header beyond the bearer token is required.

`X-Tenant-ID: <tenant uuid>` is **required** on every call; omitting it returns `400`. A
campaign outside that tenant returns `404`. See
[Tenant scoping for VB_AGENT callers](./vb_agent_tenant_scoping.md).

| Caller | Result |
|---|---|
| Valid `VB_AGENT` token | ✅ |
| Valid human JWT | ❌ 403 |
| Expired or unknown token | ❌ 401 |
| No header | ❌ 401 |

---

## ⚠️ Scoping warning

`AgentToken` carries **no tenant**, and `VB_AGENT` is a **global role** in this system — it
also reads every tenant's agents and call logs.

`X-Tenant-ID` is mandatory and scopes these endpoints to one tenant. But it is a **filter, not
an authorization boundary**: nothing stops a token holder from naming a different tenant, so **a
valid runner token can still reach any tenant's campaign**, including the full audience with
contact names and phone numbers.

Treat runner tokens as high-value credentials: store them in a secret manager, rotate them, and
never expose one to a browser.

Making this a real boundary still requires a tenant field on `AgentToken`; the header then
selects among the tenants that token is allowed. It must **not** become an agent id supplied by
the caller, which is the caller naming its own scope. Full rationale:
[Tenant scoping for VB_AGENT callers](./vb_agent_tenant_scoping.md).

---

## Endpoint index

| Method | Path | Purpose | Success |
|---|---|---|---|
| `GET` | `/{campaign_id}/context/` | Campaign, bot config, members to call | 200 |
| `POST` | `/{campaign_id}/members/update/` | Batch member outcomes | 200 |
| `POST` | `/{campaign_id}/complete/` | Declare the campaign finished | 200 |

Both use the standard envelope: `{status, message, data, errors}`, plus `meta` when paginated.

**Errors use that envelope too.** A global exception handler wraps every failure, so the DRF
detail or field map is nested under `errors` and `message` carries a displayable string:

```json
{
  "status": false,
  "message": "Invalid or inactive agent token.",
  "data": null,
  "errors": { "detail": "Invalid or inactive agent token." },
  "request_id": "5f2c9b14-…"
}
```

Nothing here returns a bare `{"detail": …}` or a bare field map — read `errors`, and log
`request_id`, which appears on errors only.

**There is no collection route.** `GET /api/v1/core/campaign-runner/` is a **404**; every path
includes a campaign id.

---

## `GET /{id}/context/`

Everything needed to run the campaign: the campaign, its bot configuration inline, and a page of
members to call.

**Available only while the campaign is `RUNNING` or `HANDING_OFF`.** Any other state → **409**.

**The first successful pull acknowledges the handoff:** `handoff_status` moves to `ACKED` and
`handed_off_at` is stamped. Later pulls do not move that timestamp, so it always records when the
runner first took the campaign.

### Query parameters

| Param | Type | Notes |
|---|---|---|
| `page` | int | 1-based. Default 1 |
| `page_size` | int | Default 10, **max 100** |

Members are ordered by `created_at, id` — stable across pages and across re-pulls.

### Response `200`

```json
{
  "status": true,
  "message": "success",
  "data": {
    "campaign": {
      "id": "3f2a…",
      "tenant_id": "9d8c…",
      "name": "Q1 Lead Qualification",
      "description": "Warm leads from the January webinar",
      "status": "RUNNING",
      "handoff_id": "9c7e…",
      "scheduled_start_at": "2027-02-15T04:00:00Z",
      "started_at": "2027-02-15T04:00:03Z",
      "total_members": 500,
      "pending_count": 470,
      "suppressed_count": 30,
      "agent": {
        "id": "b4e1…",
        "bot_name": "Sales Bot",
        "persona_name": "Aria",
        "languages": ["en-IN", "hi-IN"],
        "tone": "PROFESSIONAL",
        "voice_name": "aria-v2",
        "transfer_destinations": null
      }
    },
    "credits": {
      "balance": 1030,
      "is_frozen": false,
      "service_tier": "conversation_high_latency",
      "credits_per_minute": 5
    },
    "members": [
      {
        "id": "9f1c…",
        "full_name": "Ada Lovelace",
        "phone_e164": "+14155552671",
        "attempt_count": 0,
        "status": "PENDING"
      }
    ]
  },
  "errors": null,
  "meta": {
    "count": 470,
    "next": "http://host/api/v1/core/campaign-runner/3f2a…/context/?page=2",
    "previous": null,
    "page": 1,
    "page_size": 10
  }
}
```

### Field notes

| Field | Notes |
|---|---|
| `campaign.tenant_id` | Which tenant this belongs to. For your logging and routing |
| `campaign.handoff_id` | Stable across every delivery attempt of one launch. Use it to dedupe |
| `campaign.agent` | Full bot config inline — no second call needed |
| `agent.languages` | JSON array of BCP-47 codes |
| `agent.tone` | `PROFESSIONAL`, `FRIENDLY`, `FORMAL`, `CASUAL`, `ENTHUSIASTIC`, `RUDE` |
| `agent.transfer_destinations` | JSON or `null`. Reserved |
| `members[].id` | **The handle to echo back on every update.** Opaque — do not parse |
| `members[].phone_e164` | The number to dial, frozen at materialisation |
| `members[].attempt_count` | How many attempts this backend has recorded so far |
| `members[].status` | `PENDING` or `IN_PROGRESS` only |
| `credits.balance` | Credits the tenant has left, across both buckets |
| `credits.is_frozen` | True when spending is blocked — a lapsed cycle, or a support action |
| `credits.service_tier` | The tier this campaign's bot is billed at |
| `credits.credits_per_minute` | What a minute on that tier costs *this* tenant. `null` if the plan has no price for it |

`campaign` is repeated on every page — read it once, ignore it thereafter.

### `credits` is advisory

**Nothing here is load-bearing.** A runner that ignores it is charged exactly the same:
the authoritative refusal happens before the campaign starts, and the charge happens
when each call log lands. What it buys is the ability to pace and to surface a useful
message, instead of discovering an empty wallet one failed call at a time.

`credits` is **`null`** when the lookup failed. A billing problem must never be able to
break a handoff, so the payload arrives without it rather than not arriving.

Charging is metered before it blocks: while `credits_enforced` is off for a tenant, a
short balance is logged and the campaign runs anyway.

### Which members are returned

Only `PENDING` and `IN_PROGRESS`. So:

- `SUPPRESSED` members are **never** returned.
- `COMPLETED`, `FAILED`, and `CANCELLED` members drop out once reported.
- A re-pull after a crash resumes cleanly without re-dialling anyone already finished.

### Responses

| Code | When | `message` | `errors` |
|---|---|---|---|
| **200** | Context returned. An empty `members` array with `meta.count: 0` means everything has been reported — not an error, and the signal to stop pulling. | `success` | `null` |
| **400** | `X-Tenant-ID` missing. | `This header is required for agent tokens.` | `{"X-Tenant-ID": [...]}` |
| **400** | `X-Tenant-ID` is not a UUID, or names no active tenant. | `Must be a valid tenant UUID.` / `No active tenant matches this id.` | `{"X-Tenant-ID": [...]}` |
| **401** | Token missing, unknown, inactive, or the secret does not match. | `Invalid or inactive agent token.` / `Invalid agent token secret.` | `{"detail": …}` |
| **403** | Authenticated, but not a `VB_AGENT` — i.e. a human JWT. | `You do not have permission to perform this action.` | `{"detail": …}` |
| **404** | No such campaign, soft-deleted, or outside the tenant you named. | `Not found.` | `{"detail": …}` |
| **404** | `page` beyond the last page. | `Invalid page.` | `{"detail": …}` |
| **409** | Campaign is not `RUNNING` or `HANDING_OFF`. | `Campaign is DRAFT; there is nothing to run.` | `null` |

> **A 409 is a wait, not a failure.** A campaign still `MATERIALIZING` will become workable shortly;
> one in `PAUSED`, `COMPLETED`, or `CANCELLED` will not. Read `message` — it names the current
> status — and back off rather than retrying tightly.

> **The acknowledgement happens before the response is built**, and only on a state change. If your
> first pull succeeds but the response is lost in transit, the handoff is already `ACKED` and
> `handed_off_at` is stamped — re-pulling is still correct and will not move the timestamp.

---

## `POST /{id}/members/update/`

Report outcomes. **Each item is applied independently** — one bad row never rejects the good ones.

### Request

```json
{
  "updates": [
    {
      "member_id": "9f1c…",
      "status": "COMPLETED",
      "outcome": "answered_interested",
      "provider_call_id": "prov-abc-123",
      "attempted_at": "2027-02-15T04:11:00Z"
    },
    {
      "member_id": "7b2d…",
      "status": "FAILED",
      "error_message": "no answer after 3 attempts"
    },
    { "member_id": "4c8a…", "status": "IN_PROGRESS" }
  ]
}
```

### Request fields

`*` marks required. Every item lives inside the `updates` array.

| Field | Type | Description | Validation |
|---|---|---|---|
| `updates` * | array | The batch of per-member outcomes. | **1–1000** items. Empty → 400. Over 1000 → 400. The whole batch is rejected if any item is malformed. |
| `updates[].member_id` * | UUID | The `id` you were given in the context response. Opaque — echo it back, never parse or construct it. | Required, must be a valid UUID. An id belonging to another campaign is reported per-item as `not_found`, not as a 400. |
| `updates[].status` * | enum | What happened to this member. | Required. **Only `IN_PROGRESS`, `COMPLETED`, `FAILED`.** `PENDING`, `SUPPRESSED`, and `CANCELLED` belong to this backend and reject the **entire batch** with 400. |
| `updates[].outcome` | string | Your own disposition label, stored verbatim for reporting — e.g. `answered_interested`, `voicemail`. | Optional, max **200** characters. **Blank or omitted preserves the existing value.** |
| `updates[].error_message` | string | Why the attempt failed. Free text, no length limit. | Optional. **Blank or omitted CLEARS the stored value** — see the warning below. |
| `updates[].attempted_at` | datetime | When the attempt happened, ISO-8601. Send this if you buffer updates, so the record reflects the call time rather than the upload time. | Optional, and `null` is accepted. Omitted or `null` → server time at processing. Not validated against the campaign window; a far-past or future value is stored as given. |
| `updates[].provider_call_id` | string | Your identifier for the call, used to link the matching `AgentCallLog` to this member and contact. | Optional, max **255** characters. Blank skips linking. The log must be in the **same tenant as the campaign**; one in another tenant is not linked. No error if nothing matches — linking is best-effort. |

> **`outcome` and `error_message` behave differently on omission, and this catches people out.**
> Omitting `outcome` keeps whatever was there; omitting `error_message` **wipes** it. So a
> `FAILED` update carrying an error, followed by a `COMPLETED` retry that omits it, ends with an
> empty `error_message` — which is usually what you want, but it means you cannot rely on
> `error_message` accumulating across updates. Resend it on every update if you need it to persist.

> **`attempt_count` is maintained by this backend, not by you.** It increments on `COMPLETED` and
> `FAILED` only — never on `IN_PROGRESS` — so a member that goes `IN_PROGRESS → FAILED` records
> exactly one attempt. There is no field to set it directly.

### Response `200`

```json
{
  "status": true,
  "message": "Updates processed",
  "data": {
    "applied": 2,
    "total": 3,
    "results": [
      { "member_id": "9f1c…", "result": "applied" },
      { "member_id": "7b2d…", "result": "applied" },
      { "member_id": "0000…", "result": "not_found", "detail": "No such member in this campaign." }
    ]
  },
  "errors": null
}
```

| `result` | Meaning | Counts toward `applied`? |
|---|---|---|
| `applied` | Update took effect | ✅ |
| `ignored` | Member was already terminal — see [retry semantics](#retry-semantics) | ❌ |
| `not_found` | No such member **in this campaign** | ❌ |

> **The response is `200` even when items fail.** Read `results`, not the status code. A `403` or
> `400` means the whole batch was rejected; a `200` with failures inside means some items did not
> apply.

`results` is in the same order as `updates`.

### Side effects of an applied update

| Effect | When |
|---|---|
| Member `status`, `outcome`, `error_message`, `last_call_at` written | Every applied update |
| `provider_call_id` links the matching `AgentCallLog` to this member **and** contact | Every applied update that carries one — **not** only terminal ones |
| `attempt_count` +1 | `COMPLETED` or `FAILED` only |
| Contact's `last_contacted_at` set to `last_call_at` | `COMPLETED` only |
| Campaign counters recomputed | Once per request, if at least one item applied |

> **Call-log linking is not restricted to terminal updates.** Sending `provider_call_id` on an
> `IN_PROGRESS` update links the log immediately, which is usually what you want if you post the
> log before the call finishes.

> **Linking is silent when it does not happen.** An id that matches nothing — not yet posted,
> mistyped, or belonging to another tenant — still returns `applied`, because the member update
> itself succeeded. If the link matters to your reporting, verify it rather than inferring it from
> the result.

> **`last_contacted_at` moves only on `COMPLETED`.** A member the runner tried and failed does not
> count as contacted, so this field stays a reliable "when did we last actually reach them".

> **Counters are recomputed once per batch, not per item** — so a 1000-item batch costs one
> aggregate query, not a thousand. If every item was `ignored` or `not_found`, the recompute is
> skipped entirely.

### Status restrictions

Only `IN_PROGRESS`, `COMPLETED`, `FAILED` are accepted. `PENDING`, `SUPPRESSED`, and `CANCELLED`
belong to this backend and are rejected with **400** for the whole batch.

This is deliberate and load-bearing: accepting `PENDING` would let an external service move
somebody who opted out back into the callable set.

### Responses

The `errors` column shows the map nested under `errors` in the envelope — see
[Endpoint index](#endpoint-index).

| Code | When | `message` | `errors` |
|---|---|---|---|
| **200** | Batch processed. **Individual items may still have failed** — read `results`. | `Updates processed` | `null` |
| **400** | A status this backend owns (`PENDING`, `SUPPRESSED`, `CANCELLED`), or an unknown value. | `"PENDING" is not a valid choice.` | `{"updates": {"0": {"status": [...]}}}` |
| **400** | Empty batch. | `This list may not be empty.` | `{"updates": [...]}` |
| **400** | More than 1000 items. | `Ensure this field has no more than 1000 elements.` | `{"updates": [...]}` |
| **400** | Malformed `member_id`, or `member_id` / `status` missing from an item. | `Must be a valid UUID.` / `This field is required.` | `{"updates": {"0": {...}}}` |
| **400** | `outcome` over 200 or `provider_call_id` over 255 characters. | `Ensure this field has no more than 200 characters.` | `{"updates": {"0": {...}}}` |
| **400** | `X-Tenant-ID` missing, malformed, or naming no active tenant. | as [`context`](#get-idcontext) | `{"X-Tenant-ID": [...]}` |
| **401** | Token missing, unknown, inactive, or wrong secret. | `Invalid or inactive agent token.` | `{"detail": …}` |
| **403** | Authenticated but not a `VB_AGENT`. | `You do not have permission to perform this action.` | `{"detail": …}` |
| **404** | No such campaign, soft-deleted, or outside the tenant you named. | `Not found.` | `{"detail": …}` |

> **A field error rejects the entire batch — nothing is applied.** The index in the `errors` key
> (`"0"`, `"3"`, …) is the position in your `updates` array, so you can point at the offending item
> without re-deriving it.

> **Validation is all-or-nothing; application is per-item.** Malformed input fails the batch with a
> `400`; well-formed input that cannot be applied comes back as `200` with `not_found` or `ignored`
> in `results`. Those are different failure classes and want different handling — a `400` is a bug
> in your payload, a `not_found` is stale state.

---

## `POST /{id}/complete/`

The runner has finished with this campaign: no more calls, no more member updates. Moves the
campaign to `COMPLETED` and stamps `completed_at`.

**Nothing else sets a campaign to `COMPLETED`.** Without this call a finished campaign sits in
`RUNNING` for ever, and `completed_at` — which the campaign report prints, and measures the
campaign's duration from — stays empty.

### Request

```json
{ "completed_at": "2027-02-16T09:12:40Z" }
```

| Field | Type | Description | Validation |
|---|---|---|---|
| `completed_at` | datetime | When the runner actually finished, if that is not now. Use it when a callback is delivered late, so the record shows when the work ended rather than when the message arrived. | Optional. ISO-8601. Not more than **5 minutes** in the future — beyond that is a wrong clock, not a late callback, and is a **400**. |

An empty body (`{}`) is valid and means "finished, now".

### Response `200`

```json
{
  "status": true,
  "message": "Campaign completed",
  "data": {
    "campaign_id": "3f2a…",
    "status": "COMPLETED",
    "completed_at": "2027-02-16T09:12:40Z",
    "already_completed": false,
    "total_members": 500,
    "completed_count": 468,
    "failed_count": 2,
    "suppressed_count": 30,
    "members_left_open": 0
  },
  "errors": null
}
```

### Which statuses may be completed

| Campaign status | Result |
|---|---|
| `RUNNING`, `HANDING_OFF` | Completed. **200** |
| `COMPLETED` | Nothing changes. `already_completed: true`, and the original `completed_at` is kept. **200** |
| `CANCELLED`, `FAILED` | **409.** Terminal, and an operator's decision — not the runner's to overwrite. |
| `DRAFT`, `SCHEDULED`, `MATERIALIZING`, `PAUSED` | **409.** The runner was never handed a campaign in these states. |

### `members_left_open`

Members still `PENDING` or `IN_PROGRESS` when this call arrives: work the runner never reported an
outcome for. **They are left exactly as they are.** Marking them `FAILED` here would invent an
outcome nobody reported; the count is what makes them findable, through
`GET /campaigns/{id}/members/?status=PENDING,IN_PROGRESS`.

A non-zero value is worth an alert on the runner side — the campaign was declared finished with
work outstanding.

### Responses

| Status | When | `message` |
|---|---|---|
| **200** | Completed, or already completed. | `Campaign completed` / `Campaign was already completed` |
| **400** | `completed_at` malformed, or too far in the future. | the field error, under `errors` |
| **400** | Agent token without `X-Tenant-ID`. | `This header is required for agent tokens.` |
| **401** | Missing or invalid token. | `Authentication credentials were not provided.` |
| **403** | A human JWT. Runner endpoints are `VB_AGENT` only. | `You do not have permission to perform this action.` |
| **404** | No such campaign in the tenant named by `X-Tenant-ID`. | `Not found.` |

---

## Retry semantics

These endpoints are safe to retry. Resend a whole batch without unpicking it.

| Guarantee | Behaviour |
|---|---|
| **Terminal is write-once** | A member already `COMPLETED`, `FAILED`, `SUPPRESSED`, or `CANCELLED` returns `ignored` — not applied, not an error |
| **No double counting** | A resent batch cannot increment `attempt_count` twice |
| **Order-independent** | A late `IN_PROGRESS` after `COMPLETED` is discarded; members never walk backwards |
| **Attempts counted once** | `attempt_count` increments only on `COMPLETED` / `FAILED`, never on `IN_PROGRESS`, so `IN_PROGRESS → FAILED` records one attempt |
| **Suppressed stay suppressed** | Even a well-formed `COMPLETED` cannot revive them — `SUPPRESSED` is terminal |

**Recommended pattern:** batch outcomes and post every few seconds. On a network failure, resend
the identical batch — already-applied items come back as `ignored`.

---

## Integration walkthrough

```mermaid
sequenceDiagram
    autonumber
    participant API as Backend
    participant R as Runner

    API->>R: POST {campaign_id, tenant_id, handoff_id}
    Note over R: dedupe on handoff_id
    R-->>API: 2xx

    R->>API: GET /campaign-runner/{id}/context/?page=1
    API-->>R: 200 {campaign, agent, members[]} + meta.next
    Note over R: handoff_status → ACKED

    loop While meta.next
        R->>API: GET …?page=N
        API-->>R: 200 {campaign, members[]}
    end

    Note over R: build queue, dial

    R->>API: POST /members/update/ (IN_PROGRESS batch)
    API-->>R: 200 {applied, results[]}

    R->>API: POST /agent-call-logs/ (provider_call_id, transcript…)
    R->>API: POST /members/update/ (COMPLETED/FAILED + provider_call_id)
    API-->>R: 200 {applied, results[]}
```

1. **Pull all pages** of `/context/` before dialling. `campaign` repeats on each page.
2. **Build your own queue.** This backend does not hand out work item by item.
3. **Report `IN_PROGRESS`** when dialling starts — optional but makes the campaign view live.
4. **Post the call log** to `/agent-call-logs/` as you do today.
5. **Report the terminal status** with `provider_call_id` so the log links to the member.
6. **Re-pull `/context/`** after a crash — finished members are already excluded.

---

## Error reference

Every error uses the standard envelope and carries `request_id` — see
[Endpoint index](#endpoint-index).

| Code | Meaning | `message` | `errors` |
|---|---|---|---|
| `400` | Malformed batch, or a missing/invalid `X-Tenant-ID`. **Nothing applied.** | First field error | `{"updates": {...}}` or `{"X-Tenant-ID": [...]}` |
| `401` | Missing, unknown, inactive, or wrong-secret agent token. | `Invalid or inactive agent token.` / `Invalid agent token secret.` | `{"detail": …}` |
| `403` | Authenticated but not `VB_AGENT` — a human JWT. | `You do not have permission to perform this action.` | `{"detail": …}` |
| `404` | No such campaign, soft-deleted, outside the named tenant, or a page past the end. | `Not found.` / `Invalid page.` | `{"detail": …}` |
| `409` | Campaign not in a runnable state. **`context` only.** | `Campaign is DRAFT; there is nothing to run.` | `null` |
| `500` | Unexpected server fault. | `Something went wrong` | `null` |

Per-item failures are **not** HTTP errors — they appear in `results` on a `200`.

### How to react

| Code | Retry? | Action |
|---|---|---|
| `400` | No | Fix the payload. Retrying identical input always fails. |
| `401` / `403` | No | Credential problem. Escalate; do not loop. |
| `404` | No | The campaign is gone or you named the wrong tenant. Stop working it. |
| `409` | Yes, with backoff | Not runnable *yet*, or no longer. Check `message` for the status. |
| `500` | Yes, with backoff | Transient. Include `request_id` when reporting it. |

## Known gaps

Recorded so they are not rediscovered as bugs:

| # | Gap | Impact |
|---|---|---|
| 1 | `X-Tenant-ID` is a filter, not an authorization boundary. | Any valid runner token can read any tenant's audience by naming it. See [Scoping warning](#-scoping-warning). |
| 2 | `attempted_at` is not range-checked. | A clock-skewed runner can write `last_call_at` values outside the campaign window, and nothing rejects them. |

## Related

- [Campaigns API](campaign.md) — the human-facing side
- [Campaign Overview](../../campaigns/README.md) — architecture and status diagrams
- [Agent Call Log API](./agent_call_log.md) — where `provider_call_id` comes from
- [Tenant scoping for VB_AGENT callers](./vb_agent_tenant_scoping.md)
