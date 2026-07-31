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
- [Authentication](#authentication)
- [Scoping warning](#-scoping-warning)
- [Endpoint index](#endpoint-index)
- [`GET /{id}/context/`](#get-idcontext)
- [`POST /{id}/members/update/`](#post-idmembersupdate)
- [Retry semantics](#retry-semantics)
- [Integration walkthrough](#integration-walkthrough)
- [Error reference](#error-reference)

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

Both use the standard envelope: `{status, message, data, errors}`, plus `meta` when paginated.

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

`campaign` is repeated on every page — read it once, ignore it thereafter.

### Which members are returned

Only `PENDING` and `IN_PROGRESS`. So:

- `SUPPRESSED` members are **never** returned.
- `COMPLETED`, `FAILED`, and `CANCELLED` members drop out once reported.
- A re-pull after a crash resumes cleanly without re-dialling anyone already finished.

### Errors

| Code | Body | When |
|---|---|---|
| 401 | `{"detail": "Invalid or inactive agent token."}` | Bad, expired, or missing token |
| 403 | `{"detail": "You do not have permission to perform this action."}` | Authenticated but not `VB_AGENT` |
| 404 | `{"detail": "Not found."}` | No such campaign, or soft-deleted |
| 409 | `{"status": false, "message": "Campaign is DRAFT; there is nothing to run.", "data": null, "errors": null}` | Not `RUNNING` / `HANDING_OFF` |

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

| Field | Type | Required | Notes |
|---|---|---|---|
| `member_id` | uuid | **yes** | The `id` from the context response |
| `status` | enum | **yes** | `IN_PROGRESS`, `COMPLETED`, `FAILED` — **only these three** |
| `outcome` | string ≤200 | no | Your disposition, stored verbatim. Blank leaves the existing value |
| `error_message` | string | no | Free text |
| `attempted_at` | ISO 8601 datetime | no | Defaults to now |
| `provider_call_id` | string ≤255 | no | Links the matching `AgentCallLog` to this member |

**Batch limits:** 1–1000 updates. Empty → 400.

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

### Side effects of a `COMPLETED` update

- Member `status` → `COMPLETED`, `attempt_count` +1, `last_call_at` set.
- The contact's `last_contacted_at` is updated — usable for excluding recently-called people from
  future campaigns.
- If `provider_call_id` matches an `AgentCallLog`, that log is linked to the member and contact.
- Campaign counters are recomputed.

### Status restrictions

Only `IN_PROGRESS`, `COMPLETED`, `FAILED` are accepted. `PENDING`, `SUPPRESSED`, and `CANCELLED`
belong to this backend and are rejected with **400** for the whole batch.

This is deliberate and load-bearing: accepting `PENDING` would let an external service move
somebody who opted out back into the callable set.

### Errors

| Code | Body | When |
|---|---|---|
| 400 | `{"updates": {"0": {"status": ["\"PENDING\" is not a valid choice."]}}}` | A status this backend owns, or an unknown value |
| 400 | `{"updates": ["This list may not be empty."]}` | Empty batch |
| 400 | `{"updates": ["Ensure this field has no more than 1000 elements."]}` | Over the batch cap |
| 400 | `{"updates": {"0": {"member_id": ["Must be a valid UUID."]}}}` | Malformed id |
| 401 | `{"detail": "Invalid or inactive agent token."}` | |
| 403 | `{"detail": "You do not have permission to perform this action."}` | Not `VB_AGENT` |
| 404 | `{"detail": "Not found."}` | No such campaign |

A field error rejects the **entire batch** — nothing is applied. Per-item failures come back as
`200` with `results`.

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
    participant R as Runner
    participant API as Backend

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

| Code | Meaning | Shape |
|---|---|---|
| `400` | Malformed batch — nothing applied | DRF field map |
| `401` | Missing, invalid, or expired agent token | `{"detail": "…"}` |
| `403` | Authenticated but not `VB_AGENT` | `{"detail": "…"}` |
| `404` | No such campaign | `{"detail": "Not found."}` |
| `409` | Campaign not in a runnable state (context only) | Envelope |

Per-item failures are **not** HTTP errors — they appear in `results` on a `200`.
