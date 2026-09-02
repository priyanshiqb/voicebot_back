# Agent Call Log API Documentation

The record of what happened on a call: who called, how long it lasted, how it went, the transcript,
the mood, the probing-question answers, and a link to the recording. **Written by the voicebot,
read by humans.**

That split is the thing to understand first:

- **Only agent tokens can create.** A human JWT gets **403** on `POST` — the runtime is the sole
  author of call history, so a log always reflects a call that actually happened.
- **Nobody can update or delete.** `PUT`, `PATCH`, and `DELETE` are not routed, and `bulk_delete`
  is explicitly disabled. Call history is **append-only** by design.
- **`provider_call_id` is the correlation key.** It is globally unique and is how a log is matched
  to a campaign member and to your telephony provider's own records.

**Base path:** `/api/v1/core/agent-call-logs/`

> **VB_AGENT access:** the widest of any resource — `list`, `retrieve`, `dropdown`, `create`,
> `request_upload_url`, `confirm_upload`. Agent tokens **must** send `X-Tenant-ID: <tenant uuid>`
> (**400** if omitted). Reads are scoped to that tenant; a `create` lands in it.
> See [Tenant scoping for VB_AGENT callers](./vb_agent_tenant_scoping.md).

## Endpoints Summary

| Method | Path | Purpose | Who |
|---|---|---|---|
| `GET` | `/agent-call-logs/` | List call logs (paginated) | Users + agent tokens |
| `GET` | `/agent-call-logs/{id}/` | Retrieve one, with transcript | Users + agent tokens |
| `POST` | `/agent-call-logs/` | Create a call log | **Agent tokens only** |
| `GET` | `/agent-call-logs/dropdown/` | Label/value picker options | Users + agent tokens |
| `POST` | `/agent-call-logs/request-upload-url/` | Recording upload — step 1 | Users + agent tokens |
| `POST` | `/agent-call-logs/confirm-upload/` | Recording upload — step 2 | Users + agent tokens |

**Not available:** `PUT`, `PATCH`, and `DELETE` are unrouted (**405**), and `POST /bulk_delete/`
raises **405** explicitly rather than silently working.

> The two upload endpoints manage **call recordings**, which live on this viewset rather than one
> of their own. Full field tables and response cases are at
> [Agent Call Recordings](./agent_call_recording.md); this page covers how a recording is attached
> to a log.

---

## Posting a call log charges for it

Creating a log deducts credits from the tenant's wallet. Three things follow from that:

- **`duration_seconds` decides the charge.** A row posted with `0` or no duration is
  recorded and **not** charged — the runner may not know the length yet. It is picked
  up by a daily sweep that reports uncharged calls rather than guessing at one.
- **`service_tier` beats the agent's configured tier.** A bot set to realtime may have
  been served on the standard model — a capacity fallback, a degraded provider — and
  the tenant should pay for what actually ran. Send what served the call; omit it and
  the agent's tier is used.
- **Billing never fails the request.** The call happened and the log is committed;
  raising here would make the runner retry a POST whose row already exists, and every
  retry would be a fresh attempt to charge for the same call. A billing problem is
  ours to reconcile, not yours to retry into.

`provider_call_id` is the idempotency key for the charge as well as the row, so a
re-posted call is charged exactly once.

## Endpoint Details

### 1. List Call Logs

Paginated call history, newest call first. The list payload is deliberately light — no transcript,
no summary, no probing questions.

- **Path**: `GET /api/v1/core/agent-call-logs/`
- **Headers**: `Authorization: Bearer <token>` (plus `X-Tenant-ID` for agent tokens)

| Parameter | Type | Description | Validation |
|---|---|---|---|
| `page` | integer | Page number. Default `1`. | Beyond the last page → **404**. |
| `page_size` | integer | Rows per page. Default `10`. | Capped at `100`. |
| `search` | string | Partial, case-insensitive match on `provider_call_id`, `caller_number`, `callee_number`, `caller_name`. | Words split on spaces and **AND**ed. Both number columns are searched, so a customer's number finds their inbound *and* outbound calls. |
| `ordering` | string | Sort. Default `-started_at`. | Only `started_at` and `duration_seconds`, optionally `-` prefixed. Anything else is ignored. |
| `direction` | enum | Inbound vs outbound. | `inbound` or `outbound` — **lowercase**. |
| `outcome` | string | **Exact** match on the disposition string. Supports multiple comma-separated values (e.g. `?outcome=success,failed`). | Free text written by the runtime; there is no fixed vocabulary, so match what your runtime sends. |
| `agent` | UUID | Filter to one bot. | Must be a valid UUID. |
| `campaign_id` | string | **Exact** match on the free-text campaign tag. | A string, not a foreign key — see §3. |

- **Success Response (200 OK)**:
  ```json
  {
    "status": true,
    "message": "success",
    "data": [
      {
        "id": "7a1c…",
        "tenant": { "id": "7bf2…", "name": "Alpha Corp", "subdomain": "alpha-corp" },
        "provider_call_id": "call-001",
        "agent": { "id": "b4e1…", "bot_name": "Sales Bot" },
        "mood": "neutral",
        "caller_number": "+919388025735",
        "direction": "inbound",
        "duration_seconds": 326,
        "outcome": "order_placed",
        "started_at": "2026-06-23T17:33:31.671Z",
        "created_at": "2026-06-23T17:39:02.104Z",
        "updated_at": "2026-06-23T17:39:02.104Z",
        "deleted_at": null,
        "is_deleted": false
      }
    ],
    "errors": null,
    "meta": { "count": 1, "next": null, "previous": null, "page": 1, "page_size": 10 }
  }
  ```

> **`started_at` is nullable, so a log created without one sinks to the bottom of the default
> descending sort.** The model's secondary ordering is `-created_at`, so those rows still come back
> in a stable order rather than an arbitrary one.

| Status | When | `message` |
|---|---|---|
| **200** | Zero or more rows. | `success` |
| **400** | Unknown `direction`, or a malformed `agent` UUID. | filter error |
| **400** | Agent token without `X-Tenant-ID`. | `This header is required for agent tokens.` |
| **401** | Missing or invalid token. | `Authentication credentials were not provided.` |
| **403** | No active tenant mapping. | `Your user account is not associated with any active tenant.` |
| **404** | `page` beyond the last page. | `Invalid page.` |

---

### 2. Retrieve a Call Log

The full record, including the transcript and probing-question answers.

- **Path**: `GET /api/v1/core/agent-call-logs/{id}/`
- **Success Response (200 OK)**:
  ```json
  {
    "status": true,
    "message": "Retrieved successfully",
    "data": {
      "id": "7a1c…",
      "tenant": { "id": "7bf2…", "name": "Alpha Corp", "subdomain": "alpha-corp" },
      "provider_call_id": "call-001",
      "agent": { "id": "b4e1…", "bot_name": "Sales Bot" },
      "caller_number": "+919388025735",
      "caller_name": "Nisha Patel",
      "direction": "inbound",
      "campaign_id": "q1-leads",
      "duration_seconds": 326,
      "outcome": "order_placed",
      "mood": "neutral",
      "mood_confidence": 0.85,
      "pii_redacted": false,
      "ai_summary": "Prospect interested in a 2BHK property.",
      "preferred_language": "Spanish",
      "probing_questions_count": 3,
      "probing_questions": [
        {
          "question": "What is your monthly estimated call volume?",
          "answerSummary": "Over 50,000 calls per month.",
          "isPositive": true
        }
      ],
      "follow_up_detail": "Follow up with alternative plans after contract review.",
      "human_transfer_detail": null,
      "transcript": [
        { "id": "tl-0", "speaker": "bot", "text": "Hello! Thank you for calling.", "timestamp": 0, "isRedacted": false }
      ],
      "recording_file": {
        "id": "123e4567-e89b-12d3-a456-426614174000",
        "original_name": "call_123.mp3",
        "extension": ".mp3",
        "file_size_in_bytes": 2411008,
        "file": "https://<acct>.blob.core.windows.net/<container>/…?sv=…&sp=r&se=…&sig=…"
      },
      "started_at": "2026-06-23T17:33:31.671Z",
      "ended_at": "2026-06-23T17:38:57.671Z",
      "tags": ["urgent"],
      "created_at": "2026-06-23T17:39:02.104Z",
      "updated_at": "2026-06-23T17:39:02.104Z",
      "deleted_at": null,
      "is_deleted": false
    },
    "errors": null
  }
  ```

> **Write an id, read back an object.** `agent` and `recording_file` accept a bare UUID on write but
> are returned **expanded**. They also return `null` if the related row has been soft-deleted, so
> guard against nulls when rendering.

> **`recording_file.file` is a presigned URL valid for 24 hours.** Generated per request — do not
> cache or persist it. Without blob storage configured, the raw storage path is returned instead.

| Status | When | `message` |
|---|---|---|
| **200** | Found. | `Retrieved successfully` |
| **400** | Agent token without `X-Tenant-ID`. | `This header is required for agent tokens.` |
| **401** / **403** | As in §1. | — |
| **404** | No such log, soft-deleted, or another tenant's. | `Not found.` |

---

### 3. Create a Call Log

**Agent tokens only.** Posted by the voicebot when a call ends.

- **Path**: `POST /api/v1/core/agent-call-logs/`
- **Headers**: `Authorization: Bearer vb_…`, `X-Tenant-ID: <tenant uuid>` (**mandatory**), `Content-Type: application/json`

### Request fields

`*` marks required.

| Field | Type | Description | Validation |
|---|---|---|---|
| `provider_call_id` * | string | Your telephony provider's identifier for this call. The correlation key that links this log to a campaign member. | Required. Max **255**. **Globally unique** — re-posting the same id is a 400, so a retry after a successful write fails rather than duplicating. |
| `agent` * | UUID | The bot that handled the call. | Required. Must exist and **belong to the tenant named in `X-Tenant-ID`** — a mismatch is a **403**, not a 404. |
| `caller_number` * | string | The number that **initiated** the call: the customer on inbound, the bot on outbound. Stored as supplied; **not** normalised to E.164 here. | Required. Max **50**. |
| `callee_number` | string | The number that **received** the call: the bot on inbound, the customer on outbound. | Optional, nullable. Max **50**. See *Which number is the person* below. |
| `direction` * | enum | Who placed the call. | Required. `inbound` or `outbound`, **lowercase**. |
| `caller_name` | string | The other party's name, if known. | Optional, nullable. Max **255**. |
| `campaign_id` | string | Free-text campaign tag. **Not a foreign key** — see the note below. | Optional, nullable. Max **255**. |
| `duration_seconds` | integer | Call length in seconds. **This is what the call is billed on** — a row posted without it is not charged at all. | Optional, default `0`. Must be ≥ 0. |
| `service_tier` | enum | **How the call was actually served**, and therefore what it costs. | Optional, nullable. `static_reel`, `conversation_high_latency`, or `conversation_low_latency`. Falls back to the agent's configured tier, then to `conversation_high_latency`. |
| `outcome` | string | Your disposition label, e.g. `order_placed`. Stored verbatim; no fixed vocabulary. | Optional, nullable. Max **255**. |
| `mood` | string | Detected caller sentiment. | Optional, nullable. Max **100**. |
| `mood_confidence` | float | Confidence in `mood`, nominally 0.0–1.0. | Optional, nullable. **The range is not enforced** — `7.5` is stored as given. |
| `pii_redacted` | boolean | Whether the transcript has had PII removed. | Optional, default `false`. A flag you set; nothing verifies it. |
| `ai_summary` | string | Generated summary of the conversation. | Optional, nullable. No length limit. |
| `preferred_language` | string | Language the caller chose or was detected using. | Optional, nullable. Max **100**. |
| `probing_questions_count` | integer | How many probing questions were asked. | Optional, default `0`. **Not cross-checked** against the length of `probing_questions`. |
| `probing_questions` | JSON array | The questions asked and what was learned. Stored verbatim — no schema enforced. | Optional, default `[]`. Conventionally `{question, answerSummary, isPositive}` per entry. |
| `transcript` | JSON array | The full conversation. Stored verbatim — no schema enforced. | Optional, default `[]`. Conventionally `{id, speaker, text, timestamp, isRedacted}` per line. |
| `recording_file` | UUID | The recording, uploaded beforehand — see [Agent Call Recordings](./agent_call_recording.md). | Optional, nullable. Must be an existing recording id. |
| `follow_up_detail` | string | What follow-up is needed. | Optional, nullable. No length limit. |
| `human_transfer_detail` | string | Why and how the call was handed to a human. | Optional, nullable. No length limit. |
| `started_at` | datetime | When the call began, ISO-8601. | Optional, nullable — but the default list ordering uses it, so **send it**. |
| `ended_at` | datetime | When the call ended, ISO-8601. | Optional, nullable. **Not validated against `started_at`.** |
| `tags` | string array | Free-form labels. | Optional, default `[]`. Each max **100** characters. |

**Read-only — server-assigned:** `id`, `tenant` (taken from `X-Tenant-ID`), `created_at`,
`updated_at`, `deleted_at`, `is_deleted`.

> **`campaign_id` is a free-text string, not a link.** It exists because a real `campaign` foreign
> key cannot be added alongside it — Django derives the attribute `campaign_id` from such a key and
> the two collide. To reach the actual campaign, go through `campaign_member__campaign`. Treat this
> field as a legacy tag.

> **`transcript` and `probing_questions` are unvalidated JSON.** Any shape is accepted, including a
> bare object or a string. The structures above are conventions this system's clients follow, not a
> contract the API enforces — so consumers must read them defensively.

- **Success Response (201 Created)**: the full object from §2, with `"message": "Created successfully"`.

### Responses

| Status | When | `message` | `errors` |
|---|---|---|---|
| **201** | Log recorded. | `Created successfully` | `null` |
| **400** | `provider_call_id` already exists. | `agent call log with this provider call id already exists.` | `{"provider_call_id": [...]}` |
| **400** | `provider_call_id`, `caller_number`, or `direction` missing. | `This field is required.` | `{"<field>": [...]}` |
| **400** | `direction` is not `inbound` / `outbound`. | `"INBOUND" is not a valid choice.` | `{"direction": [...]}` |
| **400** | `agent` omitted. | `Agent must be specified.` | `{"agent": [...]}` |
| **400** | `agent` id does not exist. | `Invalid pk "…" - object does not exist.` | `{"agent": [...]}` |
| **400** | `X-Tenant-ID` missing, malformed, or naming no active tenant. | `This header is required for agent tokens.` | `{"X-Tenant-ID": [...]}` |
| **401** | Missing or invalid token. | `Authentication credentials were not provided.` | `{"detail": …}` |
| **403** | **A human JWT.** Creation is restricted to the voicebot. | `You do not have permission to perform this action.` | `{"detail": …}` |
| **403** | The `agent` belongs to a different tenant than `X-Tenant-ID`. | `The specified agent does not belong to the requested tenant.` | `{"detail": …}` |

> **The header decides the tenant, not the agent.** The row lands in the tenant named by
> `X-Tenant-ID`; the `agent` merely has to agree with it. That ordering is deliberate — writing the
> agent's tenant instead would let the caller choose its own scope by naming any agent.

> **The agent/tenant mismatch is a 403, not a 404.** Unlike object lookups, hiding it buys no
> secrecy — the header grants nothing and the caller can simply omit it — while costing the client
> an actionable error.

> **Uniqueness on `provider_call_id` is global, not per tenant.** Two tenants cannot both record a
> call with the same provider id. In practice provider ids are unique anyway; the consequence worth
> knowing is that a duplicate submission fails rather than creating a second log.

---

### 4. Dropdown

- **Path**: `GET /api/v1/core/agent-call-logs/dropdown/`
- **Query Parameters**: `limit` (≤ **1000**; over that → **400 `Limit cannot exceed 1000`**), plus all List filters and `search`.
- **Success Response (200 OK)**:
  ```json
  {
    "status": true,
    "message": "Dropdown options retrieved successfully",
    "data": [ { "label": "call-001", "value": "7a1c…" } ],
    "errors": null,
    "meta": { "count": 1, "next": null, "previous": null, "page": 1, "page_size": 10 }
  }
  ```

`label` is `provider_call_id` and `value` is the row id — so options are provider references, not
anything human-friendly. For a picker people will actually read, use List (§1) and build a label
from `caller_number` plus `started_at`.

---

### 5. Recording uploads

`POST /agent-call-logs/request-upload-url/` and `POST /agent-call-logs/confirm-upload/` live on
this viewset but manage [Agent Call Recordings](./agent_call_recording.md), where they are
documented in full.

The short version:

1. `request-upload-url` → returns `file_id`, a SAS `PUT` URL and `upload_headers`.
2. Upload the MP3 straight to blob storage, sending **every header in `upload_headers`**
   (Azure rejects the PUT without `x-ms-blob-type: BlockBlob`).
3. `confirm-upload` with `file_id` → verifies it landed and marks it `CONFIRMED`.
4. Pass that `file_id` as `recording_file` when creating the call log (§3).

---

## What this API does not expose

Two model fields are populated by other parts of the system and are **absent from every response
here**:

| Field | Set by | Why it matters |
|---|---|---|
| `contact` | Resolved from `customer_number` by E.164 matching, and by `backfill_contact_links` for older rows | The link between a call and a [Contact](./contact.md) is invisible through this API. Query from the contact side instead. |
| `campaign_member` | The [Campaign Runner](./campaign_runner.md), when it reports an outcome carrying `provider_call_id` | The link between a call and a campaign member is likewise invisible here. |

Both use `SET_NULL`, so deleting a contact or a campaign never destroys call history — but it does
silently break the association, and nothing on this page will tell you.

## Which number is the person

The customer swaps ends with the direction, so neither raw column answers "who was this call
with" on its own:

| Direction | `caller_number` | `callee_number` |
|---|---|---|
| `inbound` | the customer | the bot's DID |
| `outbound` | the bot's DID | the customer |

Every response therefore carries a derived, read-only **`customer_number`** — the human's number
whichever end they were on. **Read that, not the raw columns.** It is what contact resolution
uses, and it is the only thing that handles the wrinkle below.

> **Outbound rows come in two eras.** Before `callee_number` existed, the runner wrote the
> customer into `caller_number` regardless of direction, and those rows were deliberately left as
> they were rather than rewritten. So an outbound row with `callee_number: null` has the customer
> in `caller_number`. `customer_number` falls back accordingly; nothing else does.

`callee_number` is optional on write so the runner can adopt it on its own deploy schedule —
requiring it would reject every call log until both services shipped together. Once every runner
sends it, it can be made required and the fallback retired.

---

## Known gaps

| # | Gap | Impact |
|---|---|---|
| 1 | `contact` and `campaign_member` are not serialised. | You cannot tell from a call log which contact or campaign member it belongs to. |
| 2 | `caller_number` and `callee_number` are not normalised to E.164 on write. | Two logs for the same person can carry differently formatted numbers, and `search` matches the raw string. |
| 2b | `callee_number` is optional, and older outbound rows have none. | `customer_number` covers this, but anything reading the raw columns must handle both eras itself. |
| 3 | `mood_confidence` has no range validation. | Out-of-range values are stored and surface in reporting. |
| 4 | `probing_questions_count` is independent of `probing_questions`. | The two can disagree; trust the array's length. |
| 5 | `ended_at` is not checked against `started_at`. | A negative call duration is representable. |

## Related

- [Agent Call Recording API](./agent_call_recording.md) — the MP3 upload flow
- [Agent API](./agent.md) — the bot that handled the call
- [Contacts API](./contact.md) — who was called
- [Campaign Runner API](./campaign_runner.md) — what links a log to a campaign member
- [Tenant scoping for VB_AGENT callers](./vb_agent_tenant_scoping.md)
