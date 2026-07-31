# Campaign Module — API Implementation Plan

Companion to [campaign-module-plan.md](campaign-module-plan.md), which covers architecture and
the data model. **That layer is built, migrated (`0038`), and green** — 62 model tests, 382 core
tests, no regressions.

This document covers the API layer only: serializers, viewsets, actions, validation, and the
runner-facing endpoints.

---

## 1. What already exists

Inherited free from `BaseViewSet` ([base_model_viewset.py](../../utils/views/base_model_viewset.py))
and `TenantBaseViewSet` ([tenant_base_viewset.py](../../tenant/common/views/tenant_base_viewset.py)):

| Capability | Notes |
|---|---|
| Tenant scoping on every queryset | `get_queryset()` filters by `request.user.tenant_mapping.tenant` |
| `tenant` + `created_by` / `updated_by` injection | `perform_create` / `perform_update` |
| Response envelope | `self.success()` / `self.error()` → `{status, message, data, errors}` |
| Pagination | `SetPagination`, `page_size=10`, `max_page_size=100` |
| Search / filter / ordering backends | `DjangoFilterBackend`, `SearchFilter`, `OrderingFilter` |
| `GET /dropdown/`, `POST /bulk_delete/` | Both need care — §3.4 |
| Soft delete on `DELETE` | `perform_destroy` → `instance.soft_delete(user=...)` |

So the work is genuinely: serializers, the lifecycle actions, and validation.

---

## 2. Blocking finding — `bulk_delete` bypasses every guard

`BaseViewSet.bulk_delete` ([base_model_viewset.py:696-717](../../utils/views/base_model_viewset.py#L696-L717))
loops `obj.soft_delete(user=request.user)` directly. `Campaign` **does not override
`soft_delete`** — verified — so it inherits the unguarded `BaseModel` implementation.

Consequence: even if `destroy()` refuses to delete a `RUNNING` campaign, `POST /bulk_delete/`
with that campaign's id deletes it anyway. The runner keeps dialling an audience whose campaign
no longer exists, and no counter will ever be updated again.

**Fix at the model, not the viewset.** Add `Campaign.soft_delete()` refusing on
`LIVE_STATUSES`, exactly mirroring the `Agent.soft_delete` guard already built
([agent.py:105-129](../../core/models/agent.py#L105-L129)). Model-level because it then covers
`destroy`, `bulk_delete`, the admin, and any future service code in one place — a viewset-level
check covers only the first.

This is a **small addition to the model layer**, not API work, and should land before the
viewset. It needs its own tests alongside the existing agent-guard suite.

---

## 3. Campaign API — `/api/v1/core/campaigns/`

### 3.1 Serializers (`core/serializers/campaign.py`)

Four tiers per [AGENTS.md](../../AGENTS.md) §3.

| Serializer | Purpose |
|---|---|
| `CampaignRefSerializer` | `id`, `name`, `status`. For nesting in member/call-log payloads |
| `CampaignSerializer` | Full detail + write logic. Nested `agent` via `HybridRelatedField` |
| `CampaignListSerializer` | List view — omits `contacts`, keeps counters and `agent.bot_name` |
| `CampaignDropdownSerializer` | `{label, value}` |

Plus non-model serializers: `CampaignScheduleSerializer` (the two pickers),
`PreviewAudienceSerializer`, `CampaignStatsSerializer`.

**Read-only on the main serializer** — everything the lifecycle derives:
`status`, `scheduled_start_at`, `schedule_timezone`, `started_at`, `completed_at`,
`launched_by`, `paused_by`, `cancelled_by`, all five counters, `handoff_id`,
`handoff_status`, `handed_off_at`, `error_message`.

`start_date` and `start_time` are **write-only inputs**; `scheduled_start_at` and a computed
`start_local` are the read side (§3.3).

### 3.2 Audience — and a deliberate divergence from the Agent pattern

`Agent` handles its M2Ms with `add_x` / `remove_x` list fields, resolving each id with a
`Model.objects.get(id=...)` then comparing `.tenant`
([agent.py:186-199](../../core/serializers/agent.py#L186-L199)). That is N queries per request
and puts the tenant check in hand-written code that must not be forgotten.

**For campaigns, use a tenant-scoped queryset instead:**

```python
contacts = serializers.PrimaryKeyRelatedField(
    many=True, queryset=Contact.objects.none(), required=False
)

def __init__(self, *args, **kwargs):
    super().__init__(*args, **kwargs)
    tenant = _tenant_from_context(self.context)
    if tenant is not None:
        self.fields["contacts"].queryset = Contact.objects.filter(
            tenant=tenant, is_deleted=False
        )
```

Why this is better here, stated plainly so the divergence does not read as carelessness:

- **The cross-tenant guard cannot be forgotten.** Another tenant's contact id resolves to
  "does not exist" because it is not in the queryset. With the agent pattern, deleting the
  `if kb.tenant != active_tenant` block silently opens a cross-tenant write.
- **One query, not N.** An audience is hundreds of ids; the agent pattern is a `get()` each.
- **A campaign audience is set wholesale**, not incrementally. The wizard submits a selection.
  `add`/`remove` models a different interaction.

Capped at `CAMPAIGN_MAX_SELECTED_CONTACTS` (new setting, default 5,000) with a clean 400.
Without a cap the ceiling is `DATA_UPLOAD_MAX_MEMORY_SIZE` (11 MB), discovered as a 500.

`audience_source` accepts only `SELECTED` in this phase; `FILTER` / `UPLOAD` are rejected with
"not yet supported" rather than silently ignored.

### 3.3 Schedule input

Spec format is `DD/MM/YYYY` and `HH:MM` (24h) or `HH:MM AM/PM`.

```python
start_date = serializers.DateField(
    input_formats=["%d/%m/%Y", "%Y-%m-%d"], write_only=True, required=False, allow_null=True
)
start_time = serializers.TimeField(
    input_formats=["%H:%M", "%I:%M %p", "%H:%M:%S"], write_only=True, required=False, allow_null=True
)
```

`%d/%m/%Y` **must be listed first**. DRF tries formats in order, and `01/02/2027` parses under
both `%d/%m/%Y` and `%m/%d/%Y` — silently off by ten months if the order is wrong. ISO is kept
as a second format because it is what a machine client will send.

All validation is already implemented and tested on the model: `Campaign.set_schedule()` raises
`ValidationError` for a past instant, or for one of date/time without the other. **The serializer
must call it and translate, not re-implement it** — a second copy of the timezone logic is a
second thing to get wrong. Wrap in `try/except DjangoValidationError` and re-raise as a DRF
`ValidationError`, the pattern already used in
[contact.py](../../core/serializers/contact.py) for phone normalization.

Read side: `scheduled_start_at` (UTC), `schedule_timezone`, and `start_local`
(`{date, time}` from `Campaign.local_start`) so the pickers repopulate exactly as typed.

### 3.4 Endpoints

| Method | Path | Notes |
|---|---|---|
| `GET` | `/` | `CampaignListSerializer`. Filters + search below |
| `POST` | `/` | Creates in `DRAFT`. Pre-flight §3.5 |
| `GET` | `/{id}/` | Full detail + counters. **Poll target** during `MATERIALIZING` |
| `PATCH` / `PUT` | `/{id}/` | Field set depends on status — §3.6 |
| `DELETE` | `/{id}/` | Soft delete. **409** if live (§2) |
| `POST` | `/{id}/launch/` | `202`. → `SCHEDULED` or `MATERIALIZING` |
| `POST` | `/{id}/pause/` | `RUNNING` → `PAUSED` |
| `POST` | `/{id}/resume/` | `PAUSED` → `RUNNING` |
| `POST` | `/{id}/cancel/` | → `CANCELLED`, remaining `PENDING` members → `CANCELLED` |
| `GET` | `/{id}/members/` | Paginated. `?status=`, `?suppression_reason=`, `?search=` |
| `GET` | `/{id}/stats/` | Counters + suppression breakdown. Does not page members |
| `POST` | `/preview-audience/` | `{contact_ids}` → `{matched, callable, suppressed_by_reason}` |
| `GET` | `/dropdown/` | Inherited |
| `POST` | `/bulk_delete/` | Inherited — safe only once §2 lands |

```python
filterset_fields = ["status", "agent", "audience_source", "handoff_status", "is_active"]
search_fields = ["name", "description", "agent__bot_name"]
ordering_fields = ["created_at", "updated_at", "name", "scheduled_start_at", "status"]
ordering = ["-created_at"]
select_related_fields = ["tenant", "agent"]
prefetch_related_fields = ["contacts"]      # detail only — see below
```

`prefetch_related_fields` is set on the viewset, so it applies to **list** too. An audience of
5,000 contacts prefetched for ten campaigns per page is 50,000 rows to render a list that does
not show them. `CampaignListSerializer` must omit `contacts`, and the prefetch should be applied
per-action rather than globally — override `get_queryset()` to add it only for `retrieve`.

### 3.5 Create-time validation

Straight from your field spec. Model validators exist but **only fire on `full_clean()`**, which
DRF does not call — so each one is restated in the serializer, per [AGENTS.md](../../AGENTS.md) §3.

| Field | Rule | Error |
|---|---|---|
| `name` | required, 3–100, `^[\w\s-]+$` | 400 field error |
| `name` | unique per tenant, **case-insensitive**, excluding soft-deleted and self | 400 `"A campaign with this name already exists."` |
| `description` | optional; 3–500 if non-blank | 400 field error |
| `agent` | required, active, not deleted, same tenant | 400 |
| `start_date` / `start_time` | both or neither; combined instant in future | 400 |
| `contacts` | ≤ cap; all in tenant | 400 |

**Pre-flight — "users must have at least one active bot":** if the tenant has no active agent,
return **400** with a machine-readable `code: "NO_ACTIVE_BOT"` so the frontend can deep-link to
bot creation instead of pattern-matching an English string.

The uniqueness check is a serializer-level query **and** a DB constraint. The query gives a clean
field error; the constraint is what actually holds under concurrency. Catch `IntegrityError` in
`create()`/`update()` and convert to the same 400 — otherwise two simultaneous creates give one
user a 500.

### 3.6 Edit restrictions by status

| Status | Editable |
|---|---|
| `DRAFT` | Everything |
| `SCHEDULED` | `name`, `description`, schedule |
| `RUNNING` / `PAUSED` | `name`, `description` only |
| `MATERIALIZING` / `HANDING_OFF` | Nothing — **409** |
| Terminal | Nothing — **409** |

Changing `agent` or `contacts` after handoff would desync from what the runner already holds.
Enforced in `validate()` against `self.instance.status`, returning **409** for a status-blocked
edit and **400** for a malformed one — the request was well-formed, the state was wrong.

### 3.7 Lifecycle actions

All four follow one shape, mirroring the commit guard in
[contact_import.py:578-580](../../core/services/contact_import.py#L578-L580):

```python
with transaction.atomic():
    campaign = Campaign.objects.select_for_update().get(pk=pk, tenant=tenant)
    if campaign.status != EXPECTED:
        return self.error("...", status_code=409)
    campaign.status = NEXT
    campaign.<actor>_by = request.user
    campaign.save(update_fields=[...])
```

`select_for_update` is the double-click guard: two simultaneous launches, one 202 and one 409,
one set of members.

**`/launch/` specifically:**

1. Guard `status == DRAFT` under the lock.
2. Re-check the agent is still active (it may have been deleted since creation).
3. Resolve the audience; **409** if it is empty or entirely suppressed — a campaign that will
   call nobody is not a successful launch.
4. Generate `handoff_id` (once per launch — the idempotency key, §2.4 of the model plan).
5. Future start → `SCHEDULED`, return `202`. Otherwise → `MATERIALIZING` and enqueue.

**`/cancel/`** must also flip remaining `PENDING` members to `CANCELLED`. Use a queryset
`.update()`, not a Python loop — and note that `BaseQuerySet.delete()` is blocked
([base_model_core.py:6](../../utils/models/base_model_core.py#L6)) but `.update()` is not, so
this is safe.

### 3.8 Materialisation — the one non-API dependency

`/launch/` needs `core/services/campaigns.py::materialize_campaign(campaign_id)`, run as a Celery
task. Roughly:

```
lock campaign; assert MATERIALIZING
for batch in campaign.contacts.filter(is_deleted=False).order_by("pk").iterator(chunk_size=1000):
    with transaction.atomic():                       # savepoint per batch, not per campaign
        CampaignMember.objects.bulk_create([...], ignore_conflicts=True)
campaign.recompute_counts()
campaign.status = RUNNING if pending_count else FAILED
```

Four things that bit the contact-import commit and will bite here:

- **`bulk_create` bypasses `save()` and `perform_create`** — `tenant` must be set explicitly on
  every row. The `TenantBaseViewSet` injection is not in play inside a Celery task.
- **`ignore_conflicts=True`** makes a retried task idempotent against the partial unique
  constraint instead of raising `IntegrityError` on every re-run.
- **`order_by("pk")`** — `.iterator()` over an unordered queryset can repeat or skip rows.
- **Batch savepoints**, not one transaction around 100k rows.

`suppression_reason_for()` is already built and tested; materialisation just calls it.

---

## 4. Runner-facing API

Separate viewset, machine auth via `AgentTokenAuthentication`
([agent_token.py](../../user/authentication/agent_token.py)), already a default auth class.

**Correction to an earlier draft of this plan.** It claimed these endpoints would be
"tenant-scoped by the runner's own token". That is false, and was verified against the code:

- `AgentToken` has **no tenant field**.
- `VB_AGENT` users have **no `tenant_mapping`** — [agent_call_log.py:183-185](../../core/views/agent_call_log.py#L183-L185)
  states it outright: *"their tenant comes from the agent rather than from a tenant mapping they
  do not have."*
- `VB_AGENT` is a deliberately **global** role: it already reads every tenant's agents
  ([agent.py:133](../../core/views/agent.py#L133)) and call logs
  ([agent_call_log.py:151](../../core/views/agent_call_log.py#L151)).

**Decision: accept global read**, matching the existing posture. A valid runner token can read
any tenant's campaign audience — which is bulk contact PII, a larger exposure than agent configs
or call logs. Documented prominently in `campaign_runner.md` and in the viewset docstring.

If per-tenant scoping is wanted later, the fix is a tenant on `AgentToken`. It is **not** a check
added to these views, and **not** an `agent` id taken from the caller — that is the caller naming
its own scope, the same shape as the `object_key` hole remediated in §21 of the contact plan.

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/campaign-runner/{id}/context/` | The campaign: bot config + **non-suppressed** members, paginated |
| `POST` | `/campaign-runner/{id}/members/update/` | Batch member updates |

**The context response must never include `SUPPRESSED` members.** That is the entire point of
suppressing here rather than in the runner — filter in the queryset, not the serializer, so a
serializer change cannot leak them.

**Updates must be idempotent.** A retried batch, or two arriving out of order, must not
double-count `attempt_count` or move a `COMPLETED` member backwards:

- Terminal statuses are write-once — a late `IN_PROGRESS` after `COMPLETED` is **discarded and
  reported as accepted**, not applied and not an error.
- Only `RUNNER_STATUSES` may be set through this endpoint. `PENDING` / `SUPPRESSED` /
  `CANCELLED` are ours; accepting them here would let the runner un-suppress someone.
- Members are matched by our `campaign_member` id, falling back to `(campaign, phone_e164)`
  which the partial unique constraint guarantees is unique.

Also writes `Contact.last_contacted_at` on a `COMPLETED` update — the field exists, is exposed
read-only, and **nothing writes it today** (§6.4 of the model plan). This is where it starts.

**Open:** the exact update payload needs the runner owner's sign-off. Everything in §3 is
independent of that answer and can ship first.

---

## 5. Error contract

| Code | When |
|---|---|
| 400 | Validation failure; `NO_ACTIVE_BOT`; unsupported `audience_source` |
| 403 | No tenant mapping (raised by `TenantBaseViewSet`) |
| 404 | Another tenant's campaign — **never 403**, which would confirm it exists |
| **409** | Action or edit not allowed from the current status; concurrent launch loses the race |
| 429 | Throttle |

409-vs-400 is the distinction to hold: 400 means "your request is wrong", 409 means "your request
is fine, the campaign moved". The UI reaction differs — fix the form vs. refresh and retry.

Throttle `launch`, `pause`, `resume`, `cancel`, and `preview-audience` with a scoped
`UserRateThrottle`, following `ContactImportThrottle`
([contact_import.py:44-51](../../core/views/contact_import.py#L44-L51)). `preview-audience` is
included because it runs an unbounded `COUNT` on every keystroke if the UI debounces badly.

---

## 6. Tests (`core/tests/campaigns/`)

Added alongside the existing model tests.

**Serializer / validation** — every row of §3.5, plus:
- Duplicate name differing only in case → 400.
- **Another tenant's contact id → 400, and no campaign created.** The highest-value test here.
- Another tenant's agent id → 400.
- `01/02/2027` parses as 1 February, not 2 January (the `input_formats` order trap).
- Over the contact cap → 400, not 500.
- `audience_source=FILTER` → 400 "not yet supported".

**Lifecycle**
- Concurrent launch → one 202, one 409, one set of members.
- Launch with an all-suppressed audience → 409.
- Launch after the agent was deleted → 409.
- Editing `agent` while `RUNNING` → 409; editing `name` → 200.
- Cancel flips remaining `PENDING` → `CANCELLED`, leaves `SUPPRESSED` untouched.
- **`bulk_delete` on a live campaign → refused** (the §2 regression).

**Materialisation**
- Suppressed contacts land as `SUPPRESSED` rows with the right reason, not omitted.
- Re-running the task produces one member per contact, not two.
- Contact soft-deleted between selection and launch → excluded, launch still succeeds.

**Runner endpoints**
- Context excludes `SUPPRESSED` members.
- Runner token from tenant A on tenant B's campaign → 404.
- Same update posted twice → `attempt_count` moves once.
- Update trying to set `PENDING` or `SUPPRESSED` → 400.
- `COMPLETED` update writes `Contact.last_contacted_at`.

**Tenant isolation** — user B in the same tenant can launch/pause/cancel user A's campaign, with
`launched_by` recording B; a different tenant gets 404 throughout.

Run with `REDIS_PORT=5563 uv run python manage.py test core.tests.campaigns --settings=config.settings.test_postgres --noinput`.

---

## 7. Build order

| Phase | Contents | Gate |
|---|---|---|
| 0 | **`Campaign.soft_delete` guard (§2)** + tests | `bulk_delete` regression green |
| 1 | Serializers + validation (§3.1–3.3, §3.5) | Every §3.5 rule tested, cross-tenant test green |
| 2 | Viewset CRUD, filters, URLs, docs | CRUD + tenant-isolation green |
| 3 | `materialize_campaign` service + Celery task | Materialisation tests green |
| 4 | Lifecycle actions + `preview-audience` + `stats` | Concurrency tests green |
| 5 | Beat sweep for `SCHEDULED` campaigns | Scheduled campaign starts unattended |
| 6 | Runner context + member updates (§4) | *Blocked on payload sign-off* |

Phase 0 is a live hole and ships first. Phases 1–5 give a complete campaign module that prepares
and freezes audiences correctly with no runner attached.

---

## 8. Documentation

Per [AGENTS.md](../../AGENTS.md) §6, `docs/apiDocs/core/campaign.md` must list **every** filter
key and all allowed values — the full `status`, `audience_source`, `handoff_status`, and
`suppression_reason` enums — plus every validation rule and every error shape including the 409s.
`docs/apiDocs/core/campaign_runner.md` follows once §4 is settled. Both added to
[docs/_sidebar.md](../_sidebar.md).

---

## 9. Open questions

| # | Question | Recommendation |
|---|---|---|
| A1 | Should `/launch/` accept a schedule, or only use what is stored? | **Only stored.** Two ways to set a start time is two ways to get it wrong |
| A2 | Can a `CANCELLED` campaign be relaunched, or must it be cloned? | **Cloned.** Relaunching mutates a completed audit record |
| A3 | Does `preview-audience` need to work pre-create? | **Yes** — it is a wizard step. Hence detail-`False` |
| A4 | Should `members/` expose contact PII in full, or ids + phone only? | Full name + phone. It is the campaign's own audience, already visible via `/contacts/` |
| A5 | Runner update payload shape (§4) | Needs the runner owner |
