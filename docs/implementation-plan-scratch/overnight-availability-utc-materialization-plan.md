# Overnight availability + UTC materialization — implementation plan

**Status:** ✅ **implemented and merged.** 742 tests pass on Postgres (43 new:
`test_overnight.py` 17, `test_dst.py` 12, `test_materializer.py` 14). The DST algorithm was
verified ahead of the build against 4,415,040 generated windows across 6 timezones × 3 years, and
ships inside `Timezone.to_utc()`.

**Prerequisite:** [timezone-conversion-authority-plan.md](timezone-conversion-authority-plan.md) —
landed first (commit `891d6e7`), providing `Timezone.to_utc()`, which the materializer calls
instead of doing its own `zoneinfo` work.

### What changed from the plan during implementation

| Plan said | Shipped | Why |
|---|---|---|
| §6.4 log ERROR when a host has rules but no windows **in the queried range** | log ERROR only when a host has rules and **no future windows at all** (one extra `EXISTS`, only on the already-empty path) | The plan's version is a false-positive machine. `eligible_free_hosts` is called with a 30-minute range, so every out-of-hours booking attempt would have fired it; and a host who simply does not work on the queried date would fire it in `get_available_slots` too. A canary that cries wolf daily is worse than none. |
| §6.3 slot walk had no date filter | slots are clipped to `date_from..date_to` on the slot's **local** date | Windows are loaded ±1 day to catch overnight blocks anchored outside the range. Without the filter the engine returns slots the caller did not ask for, silently widening every response. |
| §7.1 factory defaults to `00:00 → 00:00` | done, plus a `f.materialize()` helper | Rules written through the ORM do not materialize themselves, so every scheduling test needed the extra step. Making it a named helper keeps that visible rather than buried in `make_host`. |

The engine rewrite passed all 161 pre-existing scheduling tests unchanged on the first run, which
is the evidence D8 asked for and did not have: slot alignment is preserved.

Two requirements drive this change:

1. **Overnight blocks.** `AvailabilityProfile` must express a working block that starts on
   one day and ends on the next (`22:00 → 06:00`), including bookings that straddle midnight.
2. **UTC everywhere + DST correctness.** Every interval the engine compares must be a UTC
   instant, so availability, bookings, leave, and external busy blocks are all the same kind
   of thing — and the local-offset shift across DST months must be applied correctly.

The design that satisfies both is **materialization**: authoring stays wall-clock, but every
rule is expanded into concrete UTC intervals in a new `AvailabilityWindow` table, and the
engine reads only that table.

---

## 0. Why authoring stays wall-clock (one paragraph, then we move on)

A recurring weekly rule cannot itself be stored in UTC and remain DST-correct. "09:00 Monday
in Europe/London" is `08:00Z` in winter and `09:00Z`… no — `08:00Z` in summer and `09:00Z` in
winter. One stored UTC time is wrong for half the year, in whichever half you didn't pick.
That is arithmetic, not preference.

So the split is:

| Layer | Form | Why |
|---|---|---|
| **Authoring** — `AvailabilityRule` | wall-clock `TimeField` + weekday, tz on the profile | what the user typed; survives a tz change; DST-resolvable |
| **Conversion** — `onboard.Timezone` *(§4.1)* | `to_utc(day, time)` resolved per-date | the single place DST is applied; replaces the static `offset` column |
| **Operational** — `AvailabilityWindow` *(new)* | absolute `start_utc` / `end_utc` | what the engine compares; DST already applied per-date |

The requirement "store everywhere in UTC" is met in the sense that matters: **every comparison
the booking engine performs is UTC-to-UTC.** After this change all four interval sources are
absolute UTC instants:

| Source | Meaning | Storage | Status |
|---|---|---|---|
| `AvailabilityWindow` | free | UTC | **new** |
| `Booking.effective_*` | busy | UTC | already |
| `AvailabilityException` | busy | UTC | already |
| `ExternalBusyBlock` | busy | UTC | already |

The wall-clock→UTC conversion happens exactly once, at materialization time, in one function.
Today it happens inside the slot loop on every request ([availability.py:293-296](../../core/services/availability.py#L293-L296)) — which is precisely where the midnight and DST edge cases hide.

---

## 1. Decisions (locked)

| # | Decision |
|---|---|
| **D1** | `AvailabilityRule` remains the authoring form: `weekday` + wall-clock `start_time`/`end_time`, timezone on the parent profile. |
| **D2** | New Tier-1 model `AvailabilityWindow` stores materialized **UTC** intervals. The engine reads only this table. |
| **D3** | **Wrap semantics:** `end_time <= start_time` means the block ends on the **next day**. The `availability_rule_end_after_start` check constraint is dropped. |
| **D4** | `00:00 → 00:00` therefore means exactly 24 hours. Seven such rules give true 24/7 with no gaps (half-open `[)` bounds make consecutive days contiguous). Zero-length blocks are not expressible — by design. |
| **D5** | **DST policy is explicit** (see §4.3): a nonexistent local time snaps forward to the **exact transition instant**; an ambiguous local time resolves `fold=0` for a block start and `fold=1` for a block end, so a block spanning a fall-back covers real elapsed time. A block whose entire span is consumed by the spring-forward gap produces **no window for that date**. |
| **D6** | Materialization horizon `AVAILABILITY_MATERIALIZE_DAYS` (default **90**), extended daily by a beat task. The effective horizon is `max(setting, max(EventTemplate.max_horizon_days) for the tenant)`. |
| **D7** | Regeneration triggers: rule write, profile timezone change, profile soft-delete/reactivate, daily beat task, management command. |
| **D8** | Slot generation walks **raw** windows (preserving today's per-day cursor alignment); containment checks (`_within_working_hours`) use **merged** contiguous windows, so a booking may straddle a boundary. |
| **D9** | Materialization **skips `is_active=False` profiles**. This intentionally fixes half of [internals §20.2](../flows/booking-flow-internals.md#202-is_active-is-inconsistently-honoured) as a side effect. |
| **D10** | **Greenfield: no backfill, no dual-read, no legacy path.** There is no production availability data, so `AvailabilityWindow` is authoritative from the first commit. The legacy rule-walk is deleted outright rather than kept behind a flag. |
| **D15** | **`max_horizon_days` gains `MaxValueValidator(365)`**, and `horizon_days_for()` hard-caps at the same value. Without this the field accepts 32767 (`PositiveSmallIntegerField`), and D6's `max()` would try to materialize ~90 years of windows per profile. |
| **D11** | **`onboard.Timezone` is the single conversion authority** (§4.1). It grows `to_utc()`, `utc_offset_at()`, `offset_label_at()`, `is_dst_at()`. No service constructs `ZoneInfo` directly after this change. |
| **D12** | The DST rules themselves come from **`zoneinfo`/`tzdata`**, not from a hand-maintained rules table. Rationale and evidence in §8 — this was evaluated and rejected on data, not preference. |
| **D13** | `Timezone.offset` is **redefined as a standard-time display default**, never used for arithmetic. 14 of 37 seeded zones observe DST, so it is not the offset in force for half of each year; `current_offset` on the serializers is what clients should display. |
| **D14** | `tzdata` becomes an **explicit pinned dependency**. Correctness now depends on its freshness. |

### Consequences worth stating up front

- **Availability becomes eventually-consistent with a horizon.** A rule edited today is
  correct immediately (write-path regeneration), but a booking request beyond
  `AVAILABILITY_MATERIALIZE_DAYS` sees nothing. D6's `max()` guard, bounded by D15's hard cap,
  keeps this unreachable through the API; the beat task recomputes the needed horizon each
  run, so a tenant raising `max_horizon_days` self-heals within 24 h.
- **Greenfield removes the riskiest part of this plan.** No backfill of live profiles, no
  dual-read window where two code paths must agree, no flag to retire. The change is
  additive-then-delete in a single commit.
- **A new failure mode: stale windows.** If the beat task dies, availability silently narrows
  as the horizon is consumed. §7.3 adds a staleness check to the deployment checklist.
- **Row growth.** One row per rule-occurrence per day. A host with one rule per weekday over a
  90-day horizon is 90 rows; 1,000 hosts ≈ 90k rows. Negligible, but the purge in §5.3 is not
  optional.

---

## 2. Blast radius

| Layer | Files |
|---|---|
| Models | new `core/models/availability_window.py`, [availability_rule.py](../../core/models/availability_rule.py), [core/models/__init__.py](../../core/models/__init__.py), **[onboard/models/timezone.py](../../onboard/models/timezone.py)** |
| Dependencies | [pyproject.toml](../../pyproject.toml) — pin `tzdata` (D14; currently present only transitively, so a slim container can raise `ZoneInfoNotFoundError`) |
| Migration | new `core/migrations/0039_availabilitywindow_and_more.py` |
| Services | new `core/services/availability_materializer.py`, [availability.py](../../core/services/availability.py) |
| Serializers | [availability_profile.py](../../core/serializers/availability_profile.py) |
| Tasks | [core/tasks.py](../../core/tasks.py) |
| Admin | [availability_profile_admin.py](../../core/admin/availability_profile_admin.py) |
| Settings | [config/settings/base.py](../../config/settings/base.py) — one setting + one beat entry |
| Command | new `core/management/commands/materialize_availability.py` |
| Tests | [factories.py](../../core/tests/scheduling/factories.py), [test_availability.py](../../core/tests/scheduling/test_availability.py), new `test_overnight.py`, new `test_dst.py`, new `test_materializer.py` |
| Docs | [availability_profile.md](../apiDocs/core/availability_profile.md), [booking-flow-internals.md](../flows/booking-flow-internals.md), [01-setup.md](../flows/01-setup.md), [scheduling-deployment-checklist.md](../scheduling-deployment-checklist.md) |

Everything downstream of the engine — `bookings.py`, `reschedule.py`, `rebooking.py`,
`agent_scheduling.py`, the viewsets — is **untouched**. `get_available_slots` and
`eligible_free_hosts` keep their signatures and return types.

---

## 3. Phase 1 — Models + migration

### 3.1 `core/models/availability_rule.py`

Drop the check constraint and restate the semantics:

```python
class AvailabilityRule(TenantBaseModelCore):
    """
    A single weekly working block for an AvailabilityProfile. Multiple rows per
    weekday are allowed (split shifts). Tier 1 Core.

    ``start_time`` / ``end_time`` are naive wall-clock times interpreted in the
    parent profile's timezone. When ``end_time <= start_time`` the block ends on
    the NEXT day: 22:00 -> 06:00 is an overnight shift, and 00:00 -> 00:00 is a
    full 24 hours. These wall-clock values are authoring input only — the engine
    reads the materialized UTC intervals in AvailabilityWindow.
    """

    class Meta:
        db_table = "core_availability_rules"
        ordering = ["weekday", "start_time"]
        # No end>start constraint: an overnight block legitimately has end < start.
        # Coherence is enforced by the circular-week overlap validator (§5.1).
```

`weekday` keeps its meaning: the local day the block **starts** on.

### 3.2 New `core/models/availability_window.py`

```python
class AvailabilityWindow(TenantBaseModelCore):
    """
    A materialized, absolute UTC working interval derived from one AvailabilityRule
    on one local calendar date. This is what the availability engine reads — all
    interval maths is UTC-to-UTC, with DST already resolved at materialization time.

    Pure derived data: rows are HARD-deleted and regenerated, never soft-deleted.
    Rebuild with `manage.py materialize_availability`.
    """

    profile = models.ForeignKey(
        "core.AvailabilityProfile", on_delete=models.CASCADE, related_name="windows",
    )
    rule = models.ForeignKey(
        "core.AvailabilityRule", on_delete=models.CASCADE, related_name="windows",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name="availability_windows",
        help_text="Denormalized from profile.user so the engine filters without a join.",
    )
    local_date = models.DateField(
        help_text="Local calendar date the block STARTS on. Regeneration key; also "
                  "what makes a materialization bug diagnosable.",
    )
    start_utc = models.DateTimeField()
    end_utc = models.DateTimeField()
    dst_flag = models.CharField(
        max_length=16, blank=True, default="",
        help_text="'' | 'nonexistent' | 'ambiguous' — set when a boundary landed on a "
                  "DST edge and D5 policy was applied. Diagnostic only.",
    )

    class Meta:
        db_table = "core_availability_windows"
        verbose_name = "Availability Window"
        verbose_name_plural = "Availability Windows"
        ordering = ["start_utc"]
        indexes = [
            models.Index(
                fields=["tenant", "user", "start_utc", "end_utc"],
                name="core_avail_win_user_time_idx",
            ),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["rule", "local_date"], name="unique_availability_window_occurrence",
            ),
            models.CheckConstraint(
                condition=models.Q(end_utc__gt=models.F("start_utc")),
                name="availability_window_end_after_start",
            ),
        ]
```

The `end_utc > start_utc` constraint is safe **here** (unlike on the rule) because a
materialized window is an absolute interval — the wrap has already been resolved.

Register in [core/models/__init__.py](../../core/models/__init__.py) and `__all__`.

### 3.3 ⚠ Implementation trap — bulk delete

`BaseQuerySet.delete()` **raises** by design
([base_model_core.py:6-7](../../utils/models/base_model_core.py#L6-L7)), so
`AvailabilityWindow.objects.filter(...).delete()` will blow up. Derived rows must be purged
through the plain manager:

```python
# all_objects is a plain Manager — bulk delete is intentional here: windows are
# derived cache rows with no audit value, exactly like ExternalBusyBlock.
AvailabilityWindow.all_objects.filter(...).delete()
```

### 3.4 Migration `0039`

1. `CreateModel` for `AvailabilityWindow`.
2. `RemoveConstraint(model_name="availabilityrule", name="availability_rule_end_after_start")`.
3. **No data migration.** Backfill runs as a management command post-deploy (§7.2) — a
   90-day materialization for every profile in every tenant does not belong in a migration.

---

## 4. Phase 2 — `Timezone` becomes the conversion authority, then the materializer

### 4.1 `onboard.Timezone` owns wall-clock → UTC

> **This section is now specified separately and lands first.** See
> [timezone-conversion-authority-plan.md](timezone-conversion-authority-plan.md) — it is a
> **prerequisite** for this plan, carries the authoritative model code, validator, audit
> command and tests, and reconciles the second conversion site in `campaign.py`. The summary
> below is retained for context; where the two disagree, that document wins.

Per the requirement that conversion depend on the `Timezone` model rather than on ad-hoc
`ZoneInfo(...)` calls scattered through services, all DST resolution moves **onto the model**.
`AvailabilityProfile` already points at it
([availability_profile.py:19-24](../../core/models/availability_profile.py#L19-L24)), so the
materializer calls `profile.timezone.to_utc(...)` and never touches `zoneinfo` itself.

This also fixes a **live bug**. `Timezone.offset` is a static `CharField`
([timezone.py:17-20](../../onboard/models/timezone.py#L17-L20)) holding one string per zone —
but **14 of the 37 seeded zones change offset during the year**, so that column is wrong for
roughly half of every year:

| Zone | Stored `offset` | Real (Jan) | Real (Jul) |
|---|---|---|---|
| `America/New_York` | `UTC-05:00` | −05:00 | **−04:00** |
| `Europe/London` | `UTC+00:00` | +00:00 | **+01:00** |
| `Australia/Sydney` | `UTC+10:00` | **+11:00** | +10:00 |
| `Pacific/Chatham` | `UTC+12:45` | **+13:45** | +12:45 |

…and 10 more. Anywhere that column is surfaced (it appears in the users API payload) is
already showing the wrong offset half the year.

```python
# onboard/models/timezone.py
from datetime import datetime, timedelta
from functools import cached_property
from zoneinfo import ZoneInfo

UTC = ZoneInfo("UTC")


class Timezone(BaseModelCore):
    # ... existing fields unchanged ...

    @cached_property
    def zone(self):
        """The IANA zone object. Cached per instance — ZoneInfo itself is also
        process-cached by key, so repeated construction is cheap but not free."""
        return ZoneInfo(self.timezone)

    def utc_offset_at(self, instant):
        """Real UTC offset at a given aware instant. This is the DST-correct answer;
        the stored `offset` column is a static display default and must NOT be used
        for arithmetic."""
        return instant.astimezone(self.zone).utcoffset()

    def offset_label_at(self, instant):
        """'UTC+05:30' style label for the offset actually in force at `instant`."""
        total = int(self.utc_offset_at(instant).total_seconds())
        sign = "+" if total >= 0 else "-"
        hours, minutes = divmod(abs(total) // 60, 60)
        return f"UTC{sign}{hours:02d}:{minutes:02d}"

    def is_dst_at(self, instant):
        return bool(instant.astimezone(self.zone).dst())

    def to_utc(self, day, wall_time, *, prefer_fold=0):
        """Convert a naive local (date, time) in THIS zone to an aware UTC instant.

        Returns (utc_datetime, flag) where flag is "" | "nonexistent" | "ambiguous".
        This is the single conversion point for the whole scheduling module.
        """
        aware, flag = _resolve_local(day, wall_time, self.zone, prefer_fold=prefer_fold)
        return aware.astimezone(UTC), flag
```

`offset` stays as a column — it is the sort key and the dropdown label, and dropping it would
break the onboarding UI — but it is **redefined as a standard-time display default**. §5.5 adds
a command that flags rows where it has drifted from `tzdata`.

### 4.2 The resolution algorithm

Module-level helpers in `onboard/models/timezone.py`, called only by `Timezone.to_utc`:

```python
def _transition_after(naive, tz):
    """Exact UTC instant of the DST transition that makes `naive` nonexistent.

    PEP 495: fold=1 interprets `naive` with the POST-transition offset, landing
    before the gap; fold=0 uses the PRE-transition offset, landing after it. The
    transition is therefore bracketed between the two — bisect to the second.

    Do NOT substitute "fold=0 and accept it" here. That shifts the value forward by
    the gap WIDTH rather than to the gap's END, which collides with a legitimate
    end time just past the gap and yields a zero-length window. See §7.1
    `test_gap_start_snaps_to_transition_not_gap_width`.
    """
    lo = naive.replace(tzinfo=tz, fold=1).astimezone(UTC)
    hi = naive.replace(tzinfo=tz, fold=0).astimezone(UTC)
    lo_offset = lo.astimezone(tz).utcoffset()
    while hi - lo > timedelta(seconds=1):
        mid = lo + (hi - lo) / 2
        if mid.astimezone(tz).utcoffset() == lo_offset:
            lo = mid
        else:
            hi = mid
    return hi


def _resolve_local(day, wall_time, tz, *, prefer_fold=0):
    """Convert a naive local (date, time) to an aware datetime, resolving DST edges.

    Returns (aware_datetime, flag) where flag is "" | "nonexistent" | "ambiguous".

      * NONEXISTENT (spring forward) — does not survive a UTC round-trip. Snap to
        the first valid instant, i.e. the transition itself.
      * AMBIGUOUS (fall back) — two valid offsets. `prefer_fold` selects: 0 for a
        block START, 1 for a block END, so a block spanning the repeated hour
        covers real elapsed time.
    """
    naive = datetime.combine(day, wall_time)
    dt = naive.replace(tzinfo=tz, fold=prefer_fold)

    if dt.astimezone(UTC).astimezone(tz).replace(tzinfo=None) != naive:
        return _transition_after(naive, tz), "nonexistent"

    if dt.replace(fold=0).utcoffset() != dt.replace(fold=1).utcoffset():
        return dt, "ambiguous"

    return dt, ""
```

> **Verified.** This exact implementation was exercised against 4,415,040 generated
> windows — 6 zones (`America/New_York`, `Europe/London`, `Australia/Lord_Howe` (30-min
> DST shift), `Australia/Sydney` (southern hemisphere), `Asia/Tehran`, `Asia/Kathmandu`
> (+05:45, no DST)) × 3 years × every 15-minute boundary pair including overnight wraps.
> Result: **zero** malformed windows beyond the 81 fully-in-gap cases handled below, and
> **zero** oversized windows. 7×24 h contiguity holds across transitions in all three
> DST zones tested.

### 4.3 DST policy, stated plainly

| Case | Example (`America/New_York`, 2026-03-08 / 2026-11-01) | Policy | Result |
|---|---|---|---|
| **Boundary clear, transition inside the block** | `22:00 → 06:00` across either transition | none needed — falls out of the UTC arithmetic | 7 h (spring) / 9 h (autumn) — verified |
| **Nonexistent** — spring forward, 02:00→03:00 vanishes | boundary at `02:30` | snap to the transition instant → `03:00 EDT` = `07:00Z` | `02:30→03:30` becomes a **30-minute** window |
| **Ambiguous** — fall back, 01:00–02:00 repeats | boundary at `01:30` | start → `fold=0` (first occurrence); end → `fold=1` (second) | block covers **both** occurrences: `22:00→06:00` is 9 real hours |
| **Block wholly inside the gap** | `02:00 → 03:00` or `02:15 → 02:45` on 2026-03-08 | both endpoints resolve to the transition instant → **skip, emit no window** | the block genuinely does not occur that date |

Row one is the common case for night shifts and needs no code — converting both endpoints
independently already yields the right elapsed duration. Rows two to four only fire when a
boundary itself lands on the edge.

**Row four is not a defensive afterthought.** In the 4.4M-window sweep it is the only residual
class (81 occurrences, all in spring-forward zones), and it is semantically right: a block
covering exactly the hour that never happened has nothing to materialize. It must be a
**skip with an INFO log, not an error** — and it must be skipped *before* insert, because the
`end_utc > start_utc` check constraint from §3.2 would otherwise raise `IntegrityError` and
abort the whole profile's materialization.

`dst_flag` is persisted so a support question ("why was this shift an hour short in March?")
is answerable from the row.

### 4.4 Rule → window

New module `core/services/availability_materializer.py`. Note it holds **no timezone logic of
its own** — every conversion goes through the `Timezone` model.

```python
def rule_window(rule, timezone_obj, local_day):
    """The absolute UTC interval for one rule on one local date.

    `local_day` is the day the block STARTS on. An overnight rule (end <= start)
    resolves its end against the following local date — this is the whole point.
    """
    start_utc, start_flag = timezone_obj.to_utc(local_day, rule.start_time, prefer_fold=0)

    wraps = rule.end_time <= rule.start_time
    end_day = local_day + timedelta(days=1) if wraps else local_day
    end_utc, end_flag = timezone_obj.to_utc(end_day, rule.end_time, prefer_fold=1)

    return start_utc, end_utc, (start_flag or end_flag)
```

### 4.5 Profile materialization

```python
def materialize_profile(profile, date_from, date_to):
    """Regenerate `profile`'s windows for the inclusive local-date range.

    Idempotent: the range is purged and rebuilt, so re-running is always safe and
    always converges on the current rules. Returns the number of windows written.
    """
    if profile.is_deleted or not profile.is_active:
        purge_profile(profile)                       # D9
        return 0

    tz_obj = profile.timezone            # the model, not a ZoneInfo — see §4.1
    rules = list(profile.rules.all())

    rows, day = [], date_from
    while day <= date_to:
        for rule in rules:
            if rule.weekday != day.weekday():
                continue
            start_utc, end_utc, flag = rule_window(rule, tz_obj, day)
            if end_utc <= start_utc:
                # D5 row 4: the block is wholly inside a spring-forward gap, so it
                # does not occur on this date. Expected, not an error — and it must
                # be skipped here or the end_utc>start_utc check constraint aborts
                # the whole profile.
                logger.info(
                    "skipping DST-gap window rule=%s day=%s tz=%s",
                    rule.id, day, tz_obj.timezone,
                )
                continue
            rows.append(AvailabilityWindow(
                tenant=profile.tenant, profile=profile, rule=rule,
                user_id=profile.user_id, local_date=day,
                start_utc=start_utc, end_utc=end_utc, dst_flag=flag,
            ))
        day += timedelta(days=1)

    with transaction.atomic():
        # MUST lock the profile first. Without it, two concurrent materializations of
        # the same profile+range race under READ COMMITTED (the project's default —
        # no isolation level is configured):
        #     T1 DELETE range ... T2 DELETE range (blocks on T1's row locks)
        #     T1 INSERT, COMMIT  ... T2 wakes, re-evaluates, deletes 0 rows,
        #     T2 INSERT -> IntegrityError on unique(rule, local_date)
        # Reachable whenever the write-path task (§5.2) overlaps the daily beat task
        # (§5.3), or on two rapid PATCHes of the same profile.
        AvailabilityProfile.objects.select_for_update().get(pk=profile.pk)

        AvailabilityWindow.all_objects.filter(
            profile=profile, local_date__gte=date_from, local_date__lte=date_to,
        ).delete()
        AvailabilityWindow.objects.bulk_create(rows, batch_size=1000)
    return len(rows)
```

Plus `purge_profile(profile)`, `horizon_days_for(tenant)` (implements D6's `max()`), and
`materialize_tenant(tenant, days=None)`.

**Anchoring note.** A window is anchored on the day its block *starts*. A Sunday-night
overnight rule produces a window that spills into Monday. The engine never needs a `−1 day`
fudge for this, because it queries by **interval overlap** (`start_utc < range_end AND
end_utc > range_start`), not by date. This is one of the concrete simplifications
materialization buys — compare the current `±1 day` skew window at
[availability.py:272-274](../../core/services/availability.py#L272-L274).

---

## 5. Phase 3 — Validation, write-path triggers, purge

### 5.1 Circular-week overlap validation

`AvailabilityProfileSerializer.validate_rules` currently sorts per weekday
([availability_profile.py:63-76](../../core/serializers/availability_profile.py#L63-L76)). That cannot
see a Monday-overnight rule colliding with Tuesday morning. Replace with a circular
604 800-second week timeline:

```python
WEEK_SECONDS = 7 * 24 * 3600


def _seconds(t):
    return t.hour * 3600 + t.minute * 60 + t.second


def _placed(rule):
    """(offset, end) on a 0..WEEK_SECONDS timeline. A wrapping rule extends past the end."""
    start = rule["weekday"] * 86400 + _seconds(rule["start_time"])
    length = _seconds(rule["end_time"]) - _seconds(rule["start_time"])
    if length <= 0:
        length += 86400          # D3/D4: wraps to the next day; 0 -> a full 24 h
    return start, start + length


def validate_rules(self, value):
    placed = [_placed(r) for r in value]

    total = sum(e - s for s, e in placed)
    if total > WEEK_SECONDS:
        raise serializers.ValidationError(
            "These rules cover more than a full week; they must overlap."
        )

    for i, a in enumerate(placed):
        for b in placed[i + 1:]:
            # Compare against b shifted a week either way, so Sun-night vs Mon-morning
            # is caught the same as any other pair.
            for shift in (-WEEK_SECONDS, 0, WEEK_SECONDS):
                if a[0] < b[1] + shift and b[0] + shift < a[1]:
                    raise serializers.ValidationError(
                        "Overlapping availability rules "
                        f"(weekday {value[i]['weekday']} conflicts with another block)."
                    )
    return value
```

The `total > WEEK_SECONDS` short-circuit catches over-specification (e.g. eight 24 h rules)
before the O(n²) pass, and n is at most a few dozen.

**Removed:** the per-rule `end_time > start_time` check in `AvailabilityRuleSerializer.validate`
([availability_profile.py:20-23](../../core/serializers/availability_profile.py#L20-L23)) — it is
exactly the rule that forbids overnight. Nothing replaces it; `_placed` treats
`end <= start` as a wrap and the circular check catches genuine nonsense.

### 5.2 Write-path regeneration

In `AvailabilityProfileSerializer`:

```python
def _remat(self, profile):
    from core.tasks import materialize_profile_task
    transaction.on_commit(lambda: materialize_profile_task.delay(str(profile.id)))

@transaction.atomic
def create(self, validated_data):
    rules = validated_data.pop("rules", [])
    profile = super().create(validated_data)
    self._sync_rules(profile, rules)
    self._remat(profile)
    return profile

@transaction.atomic
def update(self, instance, validated_data):
    tz_before = instance.timezone_id
    rules = validated_data.pop("rules", None)
    profile = super().update(instance, validated_data)
    if rules is not None:
        for existing in list(profile.rules.all()):
            existing.delete()
        self._sync_rules(profile, rules)
    # A timezone change re-anchors every window even when no rule changed.
    if rules is not None or profile.timezone_id != tz_before or "is_active" in validated_data:
        self._remat(profile)
    return profile
```

Also hook `perform_destroy` on `AvailabilityProfileViewSet` → purge windows on soft-delete.

### 5.3 Tasks, purge, beat

```python
@shared_task(bind=True, max_retries=3, default_retry_delay=30)
def materialize_profile_task(self, profile_id): ...

@shared_task
def materialize_availability_horizon():
    """Beat task: roll every active profile's window horizon forward and purge the past.

    Idempotent. Purge first: windows that ended over a day ago can never be read
    (every engine query is bounded below by `now`), and they are what makes the
    table grow without bound.
    """
    cutoff = timezone.now() - timedelta(days=1)
    AvailabilityWindow.all_objects.filter(end_utc__lt=cutoff).delete()
    ...
```

Settings additions:

```python
AVAILABILITY_MATERIALIZE_DAYS = env.int("AVAILABILITY_MATERIALIZE_DAYS", default=90)

CELERY_BEAT_SCHEDULE = {
    ...
    "scheduling-materialize-availability": {
        "task": "core.tasks.materialize_availability_horizon",
        "schedule": 86400.0,   # daily
    },
}
```

### 5.4 Management command

`core/management/commands/materialize_availability.py`:

```
manage.py materialize_availability [--tenant=<uuid>] [--profile=<uuid>] [--days=90] [--dry-run]
```

Used for recovery after a missed beat run and for re-verification after a timezone-data
(`tzdata`) upgrade. **Not** a migration tool — there is no existing data to backfill (D10).

### 5.5 `Timezone.offset` drift detection

Because `offset` is demoted to a display default (D13), add a command to catch it drifting from
`tzdata` in future (a zone's *standard* offset does occasionally move — Russia 2014, Samoa 2011):

```
manage.py audit_timezone_offsets [--fix]
```

For each `Timezone` row it compares the stored string against the zone's **standard-time**
offset (January in the northern hemisphere, July in the southern — or simply the minimum
offset observed across a year, which is hemisphere-agnostic and what the command should use).
Reports drift; `--fix` rewrites the column.

Run it after every `tzdata` bump, alongside `materialize_availability`. It is a reporting tool,
not a correctness dependency — nothing in the booking path reads `offset`.

---

## 6. Phase 4 — Engine rewrite

`core/services/availability.py`. Signatures and return types are unchanged.

### 6.1 Load + merge

```python
def _load_windows(tenant, host_ids, range_start, range_end):
    """{host_id: [(start_utc, end_utc), ...]} overlapping the range, ascending.

    Pure interval overlap — an overnight window anchored the previous day is picked
    up naturally, with no date fudging.
    """
    rows = (
        AvailabilityWindow.objects.filter(
            tenant=tenant, user_id__in=host_ids,
            start_utc__lt=range_end, end_utc__gt=range_start,
        )
        .order_by("user_id", "start_utc")
        .values_list("user_id", "start_utc", "end_utc")
    )
    by_host = {}
    for user_id, start, end in rows:
        by_host.setdefault(user_id, []).append((start, end))
    return by_host


def _merge(intervals):
    """Coalesce touching/overlapping intervals (input sorted by start).

    Needed so a booking may straddle two adjacent blocks — consecutive 24 h days, or
    a schedule an author chose to split (Mon 22:00-23:59:59 + Tue 00:00-06:00) rather
    than express as one wrapping rule.
    """
    merged = []
    for start, end in intervals:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged
```

### 6.2 `_within_working_hours` → `_within_working_windows`

```python
def _within_working_windows(windows, start_utc, end_utc):
    """True if [start, end] fits inside one merged working window.

    No local-date logic and no same-day restriction: a booking that straddles
    midnight is now ordinary.
    """
    return any(s <= start_utc and end_utc <= e for s, e in _merge(windows))
```

This deletes [availability.py:83-95](../../core/services/availability.py#L83-L95) outright — the
`ZoneInfo` construction, the `local_start.date() != local_end.date()` bail, and the
per-rule weekday scan all go.

### 6.3 Slot generation

Replace the day/rule double loop
([availability.py:288-304](../../core/services/availability.py#L288-L304)) with a walk over raw windows:

```python
for host_id in considered_hosts:
    busy = (_busy_intervals(...) + _exception_intervals(...) + _external_busy_intervals(...))

    for w_start, w_end in windows.get(host_id, []):
        cursor = w_start
        while cursor + duration <= w_end:
            start_utc, end_utc = cursor, cursor + duration
            if min_start <= start_utc <= horizon:
                if not _overlaps(busy, start_utc - buf_before, end_utc + buf_after):
                    slot_map.setdefault(start_utc, set()).add(host_id)
            cursor += step
```

**Raw, not merged (D8), deliberately.** The cursor resets at each window start, which is the
same instant as today's per-day rule start — so slot alignment is byte-for-byte preserved for
every existing tenant. Merging here would let a non-1440-divisor granularity (e.g. 25 min)
drift across days, which would silently move every advertised slot time.

### 6.4 No fallback — fail loud instead (D10)

Greenfield, so there is no legacy path to keep. A host with rules but **zero windows** in range
means materialization did not run or failed — a bug, not a state to paper over:

```python
windows = _load_windows(tenant, host_ids, window_start, window_end)
for host_id in considered_hosts:
    if host_id not in windows and profiles[host_id][1]:   # has rules, has no windows
        logger.error(
            "host %s has %d availability rules but no materialized windows in "
            "[%s, %s] — materialization is stale or failed",
            host_id, len(profiles[host_id][1]), window_start, window_end,
        )
```

It logs ERROR and returns no slots for that host rather than silently substituting a second
code path. Alert on this line (§7.3) — it is the canary for the stale-horizon failure mode.

Deleting `_within_working_hours` and the day/rule loop outright, with no flag and no second
implementation to keep in sync, is the main thing greenfield buys here.

### 6.5 Buffer consistency (fixes internals §20.4)

While rewriting, align `eligible_free_hosts` with `get_available_slots`: today it tests leave
and external-busy against raw `start`/`end` but bookings against `effective_*`
([availability.py:232-242](../../core/services/availability.py#L232-L242)), so a leave overlapping only
a buffer blocks at listing time and passes at create time. Both paths should build one
`busy` list and test it once against `eff_start`/`eff_end`.

---

## 7. Phase 5 — Tests, deploy, rollout

### 7.1 Tests

Existing: [factories.py](../../core/tests/scheduling/factories.py) `make_host` defaults to
`00:00–23:59` (a 1-minute nightly gap). Change to `00:00 → 00:00` for true 24/7 under D4 and
call the materializer. This makes existing tests *more* permissive, so nothing should break —
but run the full 402-test suite on Postgres before and after and diff the results.

> Scheduling tests need Postgres: `--settings=config.settings.test_postgres`. The default
> SQLite DB is broken for these models.

New `core/tests/scheduling/test_overnight.py`:

| Test | Asserts |
|---|---|
| `test_overnight_rule_materializes_one_window` | `22:00→06:00` Mon → a single 8 h UTC window anchored Mon |
| `test_slot_straddling_midnight_is_offered` | `23:30` start appears in `get_available_slots` |
| `test_booking_across_midnight_passes_working_hours` | `create_booking` at `23:30` succeeds |
| `test_full_day_rule_is_24h` | `00:00→00:00` → exactly 24 h |
| `test_seven_full_day_rules_have_no_gaps` | merged windows over 7 days form one interval |
| `test_legacy_split_pair_merges` | `Mon 22:00-23:59:59` + `Tue 00:00-06:00` → straddling booking allowed |
| `test_overlap_validator_catches_wrap_collision` | `Mon 22:00→06:00` + `Tue 02:00→09:00` → 400 |
| `test_overlap_validator_catches_sunday_to_monday` | `Sun 23:00→07:00` + `Mon 02:00→10:00` → 400 |
| `test_over_specified_week_rejected` | eight 24 h rules → 400 |

New `core/tests/scheduling/test_dst.py` — all against `America/New_York` and
`Europe/London`, with fixed dates so they never depend on "now":

| Test | Asserts |
|---|---|
| `test_spring_forward_boundary_snaps_to_transition` | `02:30` on 2026-03-08 NY → `07:00Z` (the transition), `dst_flag="nonexistent"` |
| `test_gap_start_snaps_to_transition_not_gap_width` | **regression.** `02:30→03:30` on 2026-03-08 NY → a **30-minute** window, `end > start`. Snapping by gap *width* instead of to the transition yields 0 minutes — this is the bug the naive `fold=0` implementation has |
| `test_block_wholly_in_gap_emits_no_window` | `02:00→03:00` and `02:15→02:45` on 2026-03-08 NY → zero windows for that date, INFO logged, neighbouring dates unaffected |
| `test_fall_back_boundary_covers_both_occurrences` | boundary at `01:30` → `fold=0` start / `fold=1` end, `dst_flag="ambiguous"` |
| `test_overnight_across_spring_forward_is_7_hours` | `22:00→06:00` on transition night → 7 real hours |
| `test_overnight_across_fall_back_is_9_hours` | same rule in autumn → 9 real hours |
| `test_24h_rule_is_23h_on_spring_and_25h_on_fall` | `00:00→00:00` on each transition day |
| `test_half_hour_dst_zone` | `Australia/Lord_Howe` (30-min shift) overnight block → correct elapsed time |
| `test_southern_hemisphere_transition` | `Australia/Sydney` — transitions in Apr/Oct, not Mar/Nov |
| `test_same_wall_clock_maps_to_different_utc_by_season` | `09:00` London in Jan (`09:00Z`) vs Jul (`08:00Z`) |
| `test_timezone_change_rematerializes` | PATCH profile timezone → every window's `start_utc` moves |

New `onboard/tests/test_timezone_conversion.py` — the model API from §4.1:

| Test | Asserts |
|---|---|
| `test_utc_offset_at_tracks_dst` | `America/New_York` → −05:00 in Jan, −04:00 in Jul |
| `test_offset_label_at_formats` | `Asia/Kolkata` → `"UTC+05:30"`; `Pacific/Chatham` in Jan → `"UTC+13:45"` |
| `test_is_dst_at` | true in Jul / false in Jan for `Europe/London` |
| `test_to_utc_returns_flag` | flags `""`, `"nonexistent"`, `"ambiguous"` on the right inputs |
| `test_stored_offset_column_is_not_used_for_arithmetic` | grep-style guard: `to_utc` result is unaffected by mutating `obj.offset` |
| `test_every_seeded_zone_constructs` | all 37 rows in `json_files/timezone.json` yield a valid `ZoneInfo` — catches a bad seed entry and a missing `tzdata` (D14) in CI |

New `core/tests/scheduling/test_materializer.py`:

| Test | Asserts |
|---|---|
| `test_materialize_is_idempotent` | run twice → same row count, same ids stable per `(rule, local_date)` |
| `test_concurrent_materialize_serializes` | two threads materializing the same profile+range → both succeed, no `IntegrityError`, final row count correct. Fails without the `select_for_update` in §4.5 |
| `test_inactive_profile_purged` | `is_active=False` → zero windows (D9) |
| `test_horizon_respects_max_horizon_days` | template with `max_horizon_days=120` → 120 days materialized despite the 90 default |
| `test_purge_removes_past_windows` | windows ending >1 day ago deleted |
| `test_rule_edit_regenerates` | PATCH rules → windows reflect new times |

### 7.2 Deploy runbook

No backfill step — there is no existing availability data (D10). The command exists for
recovery and for post-`tzdata` refresh, not for migration.

```
1. manage.py migrate                       # 0039
2. manage.py audit_timezone_offsets --fix  # correct the 14 stale seeded offset strings (D13)
3. Confirm the daily beat entry is registered and has run once.
4. Smoke test: create a profile with an overnight rule, assert windows appear and a
   midnight-straddling slot is offered.
```

Because windows are generated on the write path (§5.2), a profile created after deploy is
correct immediately; the beat task only extends the horizon forward.

### 7.3 Deployment checklist additions

- `scheduling-materialize-availability` present in beat and firing daily.
- Alert on `AvailabilityWindow` max `end_utc` falling below `now + max(max_horizon_days)` —
  this is the stale-horizon failure mode from §1.
- Alert on any `"falling back to rule-walk"` WARNING after step 5.
- **`tzdata` is pinned in [pyproject.toml](../../pyproject.toml)** (D14). It is currently only a
  transitive dependency — a slim container without system zoneinfo would raise
  `ZoneInfoNotFoundError` on the first materialization, which under this plan means *no
  availability at all*. `test_every_seeded_zone_constructs` catches this in CI.
- After any `tzdata` upgrade: re-run `materialize_availability` (IANA rule changes shift
  already-materialized future windows) **and** `audit_timezone_offsets`. Add both to the
  dependency-bump runbook, not just the deploy runbook — a `uv lock` refresh can move `tzdata`
  without anyone intending a timezone change.

### 7.4 Follow-up

None. Greenfield means there is no transitional scaffolding to remove later — the legacy
rule-walk is deleted in the same change that adds the materializer.

---

## 8. Rejected alternative — DST rules as data on `Timezone`

The proposal was to add transition rules to the `Timezone` model and compute offsets from
them at runtime:

| Region | Forward | Back |
|---|---|---|
| US & Canada | 2nd Sunday of March | 1st Sunday of November |
| UK & most of Europe | Last Sunday of March | Last Sunday of October |
| Australia (NSW, Vic, SA, Tas, ACT) | 1st Sunday of October | 1st Sunday of April |
| New Zealand | Last Sunday of September | 1st Sunday of April |

**The architecture was adopted** (D11 — resolution lives on `Timezone`, per-date, not a static
column). **The data source was rejected** (D12). Reasons, all measured against the actual
`json_files/timezone.json` seed and `tzdata` 2026.2:

**1. The four rules are correct today — credit where due.** All 37 seeded zones were checked;
all 14 DST-observing ones fall into one of the four buckets, with 0 uncovered. This is a
curated one-zone-per-offset seed, so the table is a genuinely accurate description of *the
zones currently in the database*. The objections below are not about today.

**2. Rules are not stable over time, and the table has no validity range.** Measured:

| Zone | 2005 | 2018 | 2026 |
|---|---|---|---|
| `America/New_York` | Apr 3 / Oct 30 | Mar 11 / Nov 4 | Mar 8 / Nov 1 |
| `America/Mexico_City` | Apr 3 / Oct 30 | Apr 1 / Oct 28 | **no DST** |
| `Asia/Tehran` | Mar 22 / Sep 21 | Mar 22 / Sep 21 | **no DST** |
| `America/Sao_Paulo` | Feb 19 / Oct 16 | Feb 17 / Nov 4 | **no DST** |
| `Europe/Moscow` | Mar 27 / Oct 30 | **no DST** | **no DST** |

The US rule changed in 2007, so "2nd Sunday of March" is wrong for **any date before 2007** —
which matters for historical bookings and audit queries. Four of five sampled zones abolished
DST outright within the last two decades. A static table cannot express "observed DST until
2019, not after"; `tzdata` does, and ships the change as a data update rather than a deploy.

**3. Transition *times* are unrepresentable in the proposed schema.** The table has dates but
no time-of-day. EU transitions fire at a single UTC instant, not at local 02:00 — verified,
all at `2026-03-29 01:00Z`:

| Zone | UTC instant | Local time |
|---|---|---|
| `Europe/London` | `01:00Z` | 02:00 |
| `Europe/Paris` | `01:00Z` | **03:00** |
| `Europe/Athens` | `01:00Z` | **04:00** |

Modelling this as "local 02:00" puts the transition an hour or two off for every EU zone that
is not UTC+0 — which silently corrupts exactly the boundary windows §4.3 exists to get right.

**4. Region is the wrong key; the rule is per-zone.** Within the named regions:
`America/Phoenix`, `Pacific/Honolulu`, `America/Regina`, `Australia/Brisbane`,
`Australia/Perth` observe **no DST at all**, and `Australia/Lord_Howe` shifts **30 minutes**,
not 60. So the rules would have to be attached per `Timezone` row — at which point it is 37+
hand-maintained rule rows duplicating what `tzdata` already ships, and growing with the seed.

**5. The seed will grow.** Adding `America/Santiago` (Chile, rules changed 2022),
`Asia/Beirut`, or `Africa/Casablanca` (Ramadan-dependent, shifts twice more per year) breaks
all four buckets immediately.

**What is kept from the proposal:** the model-centred API (D11), per-date resolution instead
of a static column (D13), and `is_dst_at()` / `offset_label_at()` for exactly the "what is the
offset right now" question that motivated it. The maintenance path becomes `pip install -U
tzdata` plus a rematerialization pass (§7.3), instead of a code change per government decree.

---

## 9. Explicitly out of scope

- **`EventTemplate.is_active` on the REST booking path** (internals §20.2, second half). D9
  fixes profiles only; the template gap lives in
  [BookingCreateSerializer](../../core/serializers/booking.py#L100) and is a separate one-line fix
  with its own API-compatibility question.
- **Per-customer timezone rendering in notifications** (internals §20.7). Related — the
  notification body is still UTC-only — but needs a `timezone` field on
  `BookingParticipant`, which is its own change.
- **`.ics` `SEQUENCE`** (internals §20.6).
- **Availability exceptions in local time.** `AvailabilityException` is already absolute UTC
  and needs nothing here.

---

## 10. Estimate

| Phase | Scope |
|---|---|
| 1 — models + migration | S |
| 2 — materializer + DST | M — the DST resolution is the subtle part; budget most of the review here. The algorithm in §4.1 is already verified against 4.4M generated windows, so the risk is in wiring, not in the maths |
| 3 — validation + triggers + command | M |
| 4 — engine rewrite | M |
| 5 — tests | L — 20 new tests, DST cases need fixed-date fixtures |
| 6 — deploy + docs | S — smaller than originally scoped: greenfield removes the backfill and the dual-read retirement |

The risk is concentrated in Phase 2. Everything else is mechanical once the windows are
correct, which is the argument for writing `test_dst.py` **first**.
