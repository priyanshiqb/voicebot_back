# Campaign Module — Architecture & Data Model

Scope of this document: **architecture and data model only.** No endpoint specs, no algorithms,
no build order.

**P0 goal: prepare the campaign.** Define it, schedule it, resolve its audience, apply
suppression, and freeze the result. How this backend hands that to the voice runner — transport,
auth, payload shape — is **deliberately undecided** and out of scope here (§2.3).

---

## 1. What the module is

A **campaign** is a named, bot-bound, scheduled outbound calling job. This backend owns four
things and persists the answers:

1. **Who** should be called — resolved from the tenant's existing contacts.
2. **Which bot** calls them.
3. **When** it starts.
4. **Who must not be called**, and why — DNC and consent suppression.

It does **not** place calls. The separate voice-runner service does that, working from the
campaign this module prepares.

---

## 2. Boundary

### 2.1 This backend cannot place calls

Verified against the repo:

- `grep` for `livekit|twilio|plivo|sip|outbound.*dial` returns nothing outside `.venv`. No
  telephony client, no SIP config, no dial primitive.
- `AgentCallLog.provider_call_id` is `unique=True` and non-nullable
  ([agent_call_log.py:11-16](../../core/models/agent_call_log.py#L11-L16)) — a call log cannot
  exist until a provider already assigned an ID, i.e. until something else placed the call.

```
  ┌────────────── voicebot_back (this repo) ──────────────┐
  │                                                        │
  │  Campaign ──▶ resolve audience                         │
  │            ──▶ apply suppression                       │
  │            ──▶ freeze → CampaignMember (PENDING)        │
  │                          │                             │
  └──────────────────────────┼─────────────────────────────┘
                             │  ← handoff (transport TBD, §2.3)
                             ▼
                      voice runner service
                             │
                             ▼
                   AgentCallLog  ──────────▶ back into this DB
                   (this path already exists today)
```

### 2.2 What each side owns

**The whole campaign is handed over. The runner builds the queue.** This backend does not hold a
work queue and does not hand out members one at a time.

| Owned here | Owned by the runner |
|---|---|
| Campaign definition, schedule, audience | **The call queue itself** |
| Suppression (DNC / consent) and its audit | Dialling, concurrency, pacing |
| Frozen record of what was handed over | Retry policy on busy / no-answer |
| Reconciling the updates that come back | Sending per-member updates back |

`CampaignMember` is therefore **not a queue** — it is our record of what we handed over and the
landing place for the runner's updates. Anything that describes *how* the runner works its queue
(lease tokens, dispatch order, concurrency) stays on their side; mirroring it here would create
two sources of truth that drift.

`attempt_count` and `outcome` **are** mirrored, because campaign reporting needs them and the
alternative is joining call logs on every read. They are copies, not authority.

### 2.3 The handoff transport is undecided

Not designed here, per your call. What the model does need is a record of *whether the handoff
happened*, and that requirement is transport-agnostic — HTTP push, message queue, or the runner
polling all need the same three facts:

| Fact | Field | Why it survives any transport choice |
|---|---|---|
| Did it go? | `handoff_status` | Distinguishes "queue built" from "runner has it" |
| When? | `handed_off_at` | Operator answer to "why hasn't it started" |
| Is this a repeat? | `handoff_id` | §2.4 |

These three go in the migration. Everything else about the handoff — URL, auth, payload,
retry cadence, whether the runner pulls or receives — is decided later and changes no schema.

### 2.4 One thing to get right whenever the transport is chosen

`handoff_id` is an **idempotency key**, regenerated once per launch and reused across every
delivery attempt of that launch.

This matters because the only outbound trigger in the repo today, `KnowledgeBase.run_etl()`
([knowledge_base.py:270-315](../../core/models/knowledge_base.py#L270-L315)), does not have one —
and it is the template someone will reach for. It fires a synchronous `requests.post(timeout=10)`
with no retry and no idempotency key. Two consequences that are tolerable for a document re-ingest
and are not tolerable here:

1. A delivery that **times out after the runner received it** cannot be safely retried. Retry it
   and you call every contact twice.
2. A single network blip marks the job failed permanently. For a campaign the user scheduled at
   09:00, that means it silently never runs.

A per-attempt UUID does not solve this — it makes retries indistinguishable from new work. The key
must be stable per launch. Flagging it now because it is a five-minute decision at model time and
an incident later.

---

## 3. Entity overview

```
  Tenant
    │
    ├──< Campaign >────────── Agent          (FK, PROTECT)
    │        │
    │        ├──── contacts M2M ────> Contact        (what the user picked)
    │        │
    │        └──< CampaignMember >──> Contact        (FK, PROTECT — what was resolved)
    │                   ▲
    │                   │
    │              AgentCallLog                      (FK — the evidence, §6)
```

Two new models.

**Why both `contacts` and `CampaignMember`.** The M2M records *what the user picked*.
`CampaignMember` records *what the system resolved at handoff time* — including the phone number as
of that moment, and everyone who was deliberately **not** called. They diverge the instant a
contact is edited, opts out, or is soft-deleted, and the campaign's results are only interpretable
against the frozen version.

---

## 4. `Campaign`

`core/models/campaign.py` — `Campaign(TenantBaseModel)`. Tier 2 (fully audited + soft delete).

### 4.1 Definition fields

| Field | Type | Notes |
|---|---|---|
| `name` | `CharField(100)` | 3–100, `^[\w\s-]+$`. Unique per tenant (§4.3) |
| `description` | `TextField(blank=True)` | Optional; 3–500 when supplied |
| `agent` | `FK → core.Agent, PROTECT` | The bot |
| `status` | `CharField(choices)` | §4.5. Server-derived, never client-writable |
| `audience_source` | `CharField(choices, default=SELECTED)` | `SELECTED` now; `FILTER` / `UPLOAD` reserved |
| `contacts` | `M2M → core.Contact` | `blank=True`, `related_name="campaigns"` |
| `audience_filter` | `JSONField(default=dict, blank=True)` | Reserved. Ships unused (§4.7) |

### 4.2 Scheduling fields

| Field | Type | Notes |
|---|---|---|
| `scheduled_start_at` | `DateTimeField(null=True, db_index=True)` | UTC (§4.4) |
| `schedule_timezone` | `CharField(64, blank=True)` | IANA name used to derive it (§4.4) |
| `started_at`, `completed_at` | `DateTimeField(null=True)` | Actuals, not plans |

### 4.3 Uniqueness

```python
constraints = [
    models.UniqueConstraint(
        Lower("name"), "tenant",
        condition=models.Q(is_deleted=False),
        name="unique_active_campaign_name_tenant",
    ),
]
indexes = [
    models.Index(fields=["tenant", "status"]),
    models.Index(fields=["status", "scheduled_start_at"]),
]
```

- **Case-insensitive.** Your spec says unique per account; a user who cannot see the difference
  between `"Q1 Lead Qualification"` and `"q1 lead qualification"` will report the duplicate as a
  bug. Expression constraints need Django ≥ 4.0 — this repo is on **6.0.5**.
- **Partial on `is_deleted=False`**, matching [agent.py:95-101](../../core/models/agent.py#L95-L101),
  so a deleted campaign does not permanently burn its name.
- `Agent.soft_delete` *also* renames `bot_name` on delete
  ([agent.py:120-123](../../core/models/agent.py#L120-L123)). That rename is redundant given the
  partial condition already excludes deleted rows — **do not copy it here.**

### 4.4 Scheduling — two pickers, one stored instant

Your spec has separate Start Date and Start Time pickers. Storing them as two columns is a trap: a
tenant that changes timezone, or a DST boundary between save and fire, silently moves the campaign.

**Decision: store one UTC instant plus the IANA name used to derive it.**

- Combine date + time in the **tenant's** timezone. `Tenant.timezone.timezone` is the IANA
  identifier ([timezone.py](../../onboard/models/timezone.py)); resolve with `ZoneInfo(...)`, the
  pattern already at [availability.py:85](../../core/services/availability.py#L85).
- Persist `scheduled_start_at` (UTC) **and** `schedule_timezone`.
- Render back as date + time in `schedule_timezone`, so the user sees what they typed.

Two model-shaping consequences:

1. "Cannot be a past date" must be evaluated on the **combined instant** in the **tenant's**
   timezone. Today's date + a time two hours ago is a past start; a date-only check in UTC passes
   it, and a tenant in `Asia/Kolkata` at 02:00 local is on the previous UTC day and gets a
   spurious rejection.
2. Date and time are **all-or-nothing**. A date with no time has no defined firing instant, and
   defaulting to midnight is how a system calls people at 00:00.

### 4.5 Status

```
DRAFT ──▶ SCHEDULED ──▶ MATERIALIZING ──▶ HANDING_OFF ──▶ RUNNING ──▶ COMPLETED
  │           │                │               │             │  ▲
  │           │                ▼               ▼             ▼  │
  │           └──────────────▶ FAILED ◀────────┘           PAUSED
  │                                                          │
  └──────────────▶ CANCELLED ◀───────────────────────────────┘
```

| State | Meaning |
|---|---|
| `DRAFT` | Created, not launched. Only state where audience and bot are editable |
| `SCHEDULED` | Launched with a future start |
| `MATERIALIZING` | Building `CampaignMember` rows. Transient |
| `HANDING_OFF` | Members built, handoff not yet confirmed |
| `RUNNING` | Runner has it. Calls are its business from here |
| `PAUSED` | Operator-held |
| `CANCELLED` | Operator-terminated |
| `FAILED` | Materialisation failed, zero callable members, or handoff gave up |
| `COMPLETED` | Derived — §6.3 |

`HANDING_OFF` is its own state because "we built the queue" and "the runner has it" are genuinely
different situations, and the gap between them is where a handoff fails. Collapsing them means a
campaign that never reached the runner still reads as `RUNNING`.

### 4.6 Handoff fields

Three fields, and only three:

| Field | Type | Purpose |
|---|---|---|
| `handoff_status` | `CharField(choices)` | `PENDING / SENT / ACKED / FAILED` |
| `handed_off_at` | `DateTimeField(null=True)` | When the runner acknowledged |
| `handoff_id` | `UUIDField(null=True, db_index=True)` | Idempotency key, §2.4 |

Rationale in §2.3–2.4. Transport-agnostic: none of these change if the transport does.

**Deliberately not included yet:** an attempt counter and a stored error string. Both describe a
retry policy, and there is no retry policy until the transport is chosen — a queue with its own
redelivery semantics needs neither, an HTTP push needs both. Adding them now would bake in an
assumption about a decision that has not been made. `handoff_status = FAILED` is enough to see
that something went wrong; the detail belongs with whatever mechanism produces it.

### 4.7 Audience representation

`SELECTED` is a plain `ManyToManyField` to `Contact`. **No through-model** — the membership audit
lives in `CampaignMember`, which records strictly more, so a second audit layer would be redundant.

`audience_filter` and the `FILTER` enum value ship in the migration **unused**. Enabling them later
is then a serializer change rather than a migration against live campaigns.

Contacts are soft-deleted, so an M2M row can outlive its contact. The selection is left alone (it
still records who was originally chosen); resolution filters `is_deleted=False`.

**Known ceiling:** picking contacts one at a time does not scale past a few hundred. With `FILTER`
deferred, that ceiling is accepted; a cap returns a clean error rather than a 500.

### 4.8 Counters

`total_members`, `pending_count`, `suppressed_count`, `completed_count`, `failed_count` —
denormalised `IntegerField(default=0)`.

The detail view is a poll target while a campaign runs, and a `GROUP BY status` over 100k members
on a two-second poll is a self-inflicted load test. All are recomputable from `CampaignMember`, so
drift is repairable rather than permanent.

---

## 5. `CampaignMember`

`core/models/campaign_member.py` — `CampaignMember(TenantBaseModelCore)`. Tier 1: machine-managed,
never hand-edited, so per-user audit columns on every row would be noise.

**This is the load-bearing table** — the frozen record of who was in scope, the compliance record
of who was deliberately not called and why, and the answer to "was this contact reached" (§6).

### 5.1 Fields

| Field | Type | Notes |
|---|---|---|
| `campaign` | `FK, CASCADE, db_index=True` | |
| `contact` | `FK → Contact, PROTECT` | |
| `phone_e164` | `CharField(20, db_index=True)` | **Frozen at materialisation.** If the contact's number is later edited, the campaign still records what it handed over |
| `status` | `CharField(choices)` | §6.3 |
| `suppression_reason` | `CharField(choices, blank=True)` | §5.2 |
| `last_call_at` | `DateTimeField(null=True)` | Denormalised from the linked call logs (§6.2) |
| `error_message` | `TextField(blank=True)` | |

```python
constraints = [
    models.UniqueConstraint(
        fields=["campaign", "contact"],
        condition=models.Q(is_deleted=False),
        name="unique_active_campaign_member",
    ),
]
indexes = [
    models.Index(fields=["campaign", "status"]),
]
```

The unique constraint is the last line of defence against handing the runner the same person
twice — including when a materialisation task is retried after a crash. It also makes
`(campaign, phone_e164)` unique, which §6.2 relies on.

### 5.2 Suppression — where DNC finally lands

§14 of the [contact plan](contact-module-plan.md) says explicitly:

> **DNC enforcement belongs to the caller, not here.** This module records `do_not_call` and
> `consent_status`; the voice runtime must query and honour them before dialling.

That deferral resolves **here**. The runner receives a list that has already had opt-outs removed,
so honouring DNC is not a behaviour it has to implement correctly.

```
suppression_reason ∈ { DO_NOT_CALL, CONSENT_DENIED, NO_PHONE, CONTACT_DELETED }
```

1. **`SUPPRESSED` is a distinct status, not `FAILED`.** "We did not call this person because they
   opted out" and "we tried and it failed" are different answers to a regulator. Collapsing them
   makes the compliance report unbuildable.
2. **Suppressed members are retained as rows**, not filtered out at materialisation. "Why wasn't
   this person called" must be answerable from the campaign alone, without cross-referencing the
   contact's *current* state — which may have changed since.
3. **`ConsentStatus.UNKNOWN` does not suppress.** Every contact starts `UNKNOWN`
   ([contact.py:70-74](../../core/models/contact.py#L70-L74)), so suppressing it would make every
   campaign call nobody. **Policy question for your compliance owner, not an engineering default.**
   If the answer is "UNKNOWN must block", it becomes a per-campaign `require_explicit_consent`
   boolean — a field on `Campaign`, so it needs deciding before the migration.

**The gap this creates.** Suppression is applied once, at materialisation. Between then and the
runner's last call, someone can opt out — and the runner is working from a snapshot that no longer
reflects it. The window is as long as the campaign takes to run. Closing it needs the runner's
owner in the room; flagging it as the largest compliance exposure in this design so it is not
quietly lost.

---

## 6. "Is this contact called or not?" — your question

Short answer: **`CampaignMember.status` is the answer; `AgentCallLog` is the evidence.** You need
both, and the link between them must be explicit rather than inferred.

### 6.1 There are three different questions hiding in this one

| Question | Answered by | Exists today? |
|---|---|---|
| In *this* campaign, was this contact called? | `CampaignMember.status` | New |
| What actually happened on the call? | `AgentCallLog` rows linked to that member | Model exists |
| Have we *ever* contacted this person? | `Contact.last_contacted_at` | **Field exists — nothing writes it** (§6.4) |

They are not interchangeable. The third is what you want when building the *next* campaign
("don't call anyone we spoke to last week"); the first is what you want on the campaign report.

### 6.2 Why not just derive it from `AgentCallLog`

Deriving is tempting — the call logs are already there. Three reasons it does not work:

1. **It cannot express the states that matter most.** No call log will ever exist for a
   `SUPPRESSED` member, or a `CANCELLED` one, or one still `PENDING`. Absence of a row is ambiguous
   between "not yet", "never will be", and "we deliberately refused" — and the third is precisely
   the one a compliance reviewer asks about.
2. **It is wrong, not just slow.** If you match a contact to a campaign by *contact + time
   window*, an **inbound** call from that person during the campaign — or a call from an unrelated
   campaign running concurrently — falsely counts as "we contacted them". `AgentCallLog` carries
   both directions ([agent_call_log.py:7-9](../../core/models/agent_call_log.py#L7-L9)), so this
   is a live failure mode, not a hypothetical.
3. **Cost.** The campaign detail view is a poll target; joining against `AgentCallLog` on every
   poll over 100k members is a self-inflicted load test.

So the link must be **explicit and recorded**, never inferred from timing.

### 6.3 The link, and who writes what

Add `campaign_member` FK to `AgentCallLog` alongside the `campaign` FK (§7.1). Then:

- `member.call_logs` — the evidence, **plural**, because the runner may retry a number.
- `CampaignMember.status` — the denormalised answer.
- `CampaignMember.last_call_at` — for sorting and "when did we last try".

```
PENDING      — handed over, awaiting an outcome        ← ours
SUPPRESSED   — excluded, never handed over             ← ours
CANCELLED    — cancelled before it was reached         ← ours
IN_PROGRESS  — runner reported dialling                ← mirrored
COMPLETED    — runner reported a finished call         ← mirrored
FAILED       — runner reported failure                 ← mirrored
```

| Transition | Written by | Trigger |
|---|---|---|
| *(create)* → `PENDING` / `SUPPRESSED` | This backend | Materialisation at launch |
| → `CANCELLED` | This backend | Operator cancels |
| → `IN_PROGRESS` / `COMPLETED` / `FAILED` | This backend | **Reconciling an inbound runner update** |

The runner never writes this table directly — it sends member updates, and this backend applies
them. That keeps tenant scoping, status validation, and counter maintenance in one place instead
of trusting an external service to get them right.

**Suppressed members are never handed over.** The runner receives only the `PENDING` set, so
honouring DNC is not a behaviour it has to implement correctly (§5.2).

**How an update finds its member.** Two options, in preference order:

1. **The runner echoes back the `campaign_member` id** it was given. Deterministic, no matching
   logic, no ambiguity. Preferred — it costs the runner one opaque field it never has to
   interpret.
2. **Match on `(campaign, phone_e164)`.** Works because §5.1's unique constraint guarantees one
   member per contact per campaign, and `phone_e164` is frozen at materialisation so it cannot
   drift out from under the match. Viable fallback if the runner issues its own ids instead —
   `external_ref` exists to store those.

Never match on contact + time window, for the reason in §6.2.2.

**Updates must be idempotent.** A retried update, or two updates arriving out of order, must not
double-count `attempt_count` or move a `COMPLETED` member backwards. Terminal statuses are
write-once; a late `IN_PROGRESS` arriving after `COMPLETED` is discarded, not applied.

### 6.4 `Contact.last_contacted_at` is currently dead

[contact.py:84-88](../../core/models/contact.py#L84-L88) declares it, and the serializer exposes it
read-only ([contact.py:28,56](../../core/serializers/contact.py#L28)). Its help text says
*"Maintained by the voice runtime, not by this module."*

**Nothing writes it.** A repo-wide grep for `last_contacted_at` outside migrations returns exactly
three hits: the model declaration and two serializer references. It has been null since it was
added.

That is the field your cross-campaign question wants. **Recommendation: the campaign module starts
writing it** when a call log links to a member. It is one assignment in the same code path that
closes out the member, and it turns a dead column into the answer to "don't call anyone we already
reached".

Once it is populated, "exclude contacts contacted in the last N days" becomes a filter on
`ContactFilter` — which today has no date lookups at all
([contact.py:30-53](../../core/views/contact.py#L30-L53)), so that is a small additive change when
you want it. Noting it as enabled-by-the-model; the filter itself is an API detail for later.

---

## 7. Changes to existing models

| Model | Change | Why |
|---|---|---|
| `AgentCallLog` | Add `campaign_member` FK only — see §7.1 | §7.1 |
| `Agent` | Guard `soft_delete` against non-terminal campaigns | §7.2 |
| `Contact` | Nothing structural — start writing `last_contacted_at` | §6.4 |

### 7.1 `AgentCallLog.campaign` and `.campaign_member`

[agent_call_log.py:50-55](../../core/models/agent_call_log.py#L50-L55) already has an
unconstrained `campaign_id = CharField(max_length=255, null=True)` — exposed in the serializer
([agent_call_log.py:37](../../core/serializers/agent_call_log.py#L37)) and used as a filter key
([agent_call_log.py:44](../../core/views/agent_call_log.py#L44)). It is clearly where the runner
already tags campaign calls, with no referential integrity behind it.

Add `campaign_member` as a `SET_NULL` FK, so deleting a campaign never destroys call history
(same reasoning as the `contact` FK added in D12 of the contact plan).

**Only that one FK — and this was forced, not chosen.** The earlier draft also called for a
`campaign` FK, denormalised for direct filtering. It cannot be added while `campaign_id` exists:
Django derives the attribute `campaign_id` from a field named `campaign`, and the two collide at
both the Python and column level (`models.E006`). Verified with `manage.py check`.

The options were:

| Option | Verdict |
|---|---|
| Rename `campaign_id` → `legacy_campaign_id` | **No.** A rename of a live column the runner writes to today, and `makemigrations` non-interactively emits Remove+Add — which drops the data |
| Name the FK something else (`campaign_ref`) | **No.** A permanently awkward name to buy a convenience filter |
| **Add `campaign_member` only** | **Yes.** Purely additive. Reach the campaign via `campaign_member__campaign` |

The cost is one extra join and a slightly worse filter contract. That is a fair price for a
migration that cannot lose data. When the runner stops writing the free-text tag, drop
`campaign_id` and add the `campaign` FK cleanly then.

Linking a call log to a member still requires the runner to change — it must send either our
`campaign_member` id or enough to match on `(campaign, phone_e164)`. Coordinate that cutover.

### 7.2 `Agent.soft_delete` guard

Today it renames and flags with no reference check
([agent.py:103-129](../../core/models/agent.py#L103-L129)). Deleting a bot that a live campaign
depends on leaves the runner dialling with stale config, or failing outright.

`PROTECT` on the FK does not help — agents are *soft*-deleted, so the constraint never fires. The
guard has to be explicit in `soft_delete`.

**This is a behaviour change to existing code**, small but not additive. Flagging it as such.

---

## 8. Open decisions

| # | Question | Recommendation |
|---|---|---|
| D1 | Does `ConsentStatus.UNKNOWN` block dialling? (§5.2) | **No** by default. If yes, `require_explicit_consent` goes on `Campaign` now — schema, not config |
| D2 | Can the runner echo back a `campaign_member` id? (§6.3) | **Ask for it.** Fallback exists, but the echo removes all matching logic |
| D3 | What identifier do runner updates carry — our `campaign_member` id, or their own? (§6.3) | Either works. If theirs, they land in `external_ref`; if neither, we fall back to `(campaign, phone_e164)` |
| D4 | "Active bots created **by the user**" — user- or tenant-scoped? | **Tenant-scoped.** Every other selector here is; user-scoping means a colleague cannot maintain your campaign, contradicting the tenant-wide decision (D10) taken for contact imports |
| D5 | Can a campaign be edited while `RUNNING`? | **Name and description only.** The runner already holds the audience; changing it here would silently desync |
| D6 | Are there wizard Steps 3+ (retry, calling hours, pacing)? | If the **runner** owns dial-time behaviour (§2.2), these are its config, not this backend's — left off §4 deliberately. Add back only if this backend passes them through |
| D7 | Multiple campaigns targeting the same contact simultaneously? | **Allowed at the model level.** Preventing it is a cross-campaign rule needing its own design |

---

## 9. Migration shape

One migration, `core`:

1. `CREATE TABLE core_campaign` + M2M through table + 2 indexes + 1 expression constraint.
2. `CREATE TABLE core_campaign_member` + 1 index + 1 partial unique constraint.
3. `ALTER TABLE core_agent_call_log` — add `campaign` and `campaign_member` FKs (both nullable,
   indexed).

No data migration, no backfill, nothing long-running or destructive — deploys without a
maintenance window. No new settings: the handoff transport that would need them is undecided (§2.3).

Tests must run with `--settings=config.settings.test_postgres` — `Contact.tags` and
`AgentCallLog.tags` are `ArrayField`, which SQLite cannot host.
