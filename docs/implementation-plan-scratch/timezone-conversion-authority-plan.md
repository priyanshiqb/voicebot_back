# `Timezone` as the conversion authority — implementation plan

**Status:** 📋 proposed. Lands **independently of and before** the
[overnight availability work](overnight-availability-utc-materialization-plan.md), which depends
on it.

**Scope.** Make `onboard.Timezone` the single place wall-clock ↔ UTC conversion happens, with
DST resolved per-date from `tzdata`. This is "option 2" from the timezone comparison: consume
IANA data through `zoneinfo`, expose it as model methods, and delete the ad-hoc conversions
scattered across services.

**Not in scope.** Live-patchable DST overrides (the "option 3" override table). Deliberately
deferred — see §9.

---

## 1. Why this lands first

Three defects exist today, independent of any other work:

| # | Defect | Effect |
|---|---|---|
| 1 | `Timezone.offset` is a static `CharField` | **14 of 37 seeded zones observe DST**, so the stored string is not the offset in force for ~half of every year. Surfaced in the users API and the timezone picker. |
| 2 | `Timezone.timezone` has no validator | One bad row → `ZoneInfoNotFoundError` → **500** on availability |
| 3 | No nonexistent/ambiguous handling anywhere | DST edge instants silently resolve wrong |

And two callers each solve the same problem separately, with **different policies**:

- [availability.py:85,281](../../core/services/availability.py#L85) — `ZoneInfo(profile.timezone.timezone)`, no DST edge handling at all
- [campaign.py:40-52](../../core/models/campaign.py#L40-L52) — `combine_local()`, documented `fold=0` policy, soft fallback to UTC on a bad zone

Nothing forces them to agree. §4 makes them.

---

## 2. Decisions

| # | Decision |
|---|---|
| **T1** | `Timezone` gains `zone`, `utc_offset_at()`, `offset_label_at()`, `is_dst_at()`, `to_utc()`, `standard_offset()`. |
| **T2** | `Timezone.timezone` gains an IANA validator. Invalid names are rejected at **write** time (400), not discovered at read time (500). |
| **T3** | `Timezone.offset` is **retained but redefined** as a standard-time display string. Never used for arithmetic. A command detects drift. |
| **T4** | **One DST policy, codebase-wide** — nonexistent snaps to the transition instant; ambiguous is caller-selected via `prefer_fold`. This *changes* campaign behaviour slightly; see §4.2. |
| **T5** | `to_utc()` returns `(utc_datetime, flag)`. Callers that don't care ignore the flag; callers that persist derived rows store it. |
| **T6** | `tzdata` becomes an explicit pinned dependency. |
| **T7** | Serializers expose `current_offset` (live) alongside `offset` (static). Additive — no existing field changes shape. |

---

## 3. Phase 1 — the model

### 3.1 Validator

```python
# onboard/models/timezone.py
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from django.core.exceptions import ValidationError


def validate_iana_timezone(value):
    """Reject anything `zoneinfo` cannot resolve.

    Write-time rejection matters because the read-time failure is a 500 three layers
    away in the availability engine, on behalf of every host in the zone.
    """
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValidationError(
            f"{value!r} is not a valid IANA timezone identifier."
        ) from exc
```

Applied to the field:

```python
timezone = models.CharField(
    max_length=100,
    unique=True,
    validators=[validate_iana_timezone],
    help_text="Standard IANA timezone identifier (e.g., Europe/London).",
)
```

> Validators run in `full_clean()`, which DRF invokes but bare `.save()` does not. The seed
> loader and any management command that writes `Timezone` rows must call `full_clean()`, or
> the guard is decorative. `test_every_seeded_zone_constructs` (§7) is the backstop.

### 3.2 Resolution helpers

Module-level, called only by `Timezone.to_utc`:

```python
from datetime import datetime, timedelta

UTC = ZoneInfo("UTC")


def _transition_after(naive, tz):
    """Exact UTC instant of the DST transition that makes `naive` nonexistent.

    PEP 495: fold=1 interprets `naive` with the POST-transition offset, landing
    BEFORE the gap; fold=0 uses the PRE-transition offset, landing AFTER it. The
    transition is bracketed between them — bisect to the second.

    Do NOT replace this with "use fold=0 and accept it", and do NOT substitute
    dateutil's resolve_imaginary(): both shift by the gap WIDTH rather than to the
    gap's END. For a single instant that is harmless, but for an interval it
    collides with an end time just past the gap and produces a ZERO-LENGTH window.
    Verified: 02:30->03:30 on 2026-03-08 America/New_York.
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
    """Naive local (date, time) -> aware datetime, resolving DST edges.

    Returns (aware_datetime, flag): "" | "nonexistent" | "ambiguous".
    """
    naive = datetime.combine(day, wall_time)
    dt = naive.replace(tzinfo=tz, fold=prefer_fold)

    if dt.astimezone(UTC).astimezone(tz).replace(tzinfo=None) != naive:
        return _transition_after(naive, tz), "nonexistent"

    if dt.replace(fold=0).utcoffset() != dt.replace(fold=1).utcoffset():
        return dt, "ambiguous"

    return dt, ""
```

> **Verified.** This exact code was exercised against **4,415,040** generated intervals —
> 6 zones (`America/New_York`, `Europe/London`, `Australia/Lord_Howe` (30-min DST shift),
> `Australia/Sydney` (southern hemisphere), `Asia/Tehran`, `Asia/Kathmandu` (+05:45, no DST))
> × 3 years × every 15-minute boundary pair including overnight wraps. Zero malformed results;
> the only residual class is intervals wholly inside a spring-forward gap, which correctly
> resolve to zero length and must be skipped by the caller.

### 3.3 Model methods

```python
class Timezone(BaseModelCore):
    # existing fields, plus the validator from 3.1

    @property
    def zone(self):
        """The IANA zone object.

        A plain property, not cached_property: ZoneInfo already caches by key at the
        module level, so construction is a dict lookup. cached_property would also
        go stale if `self.timezone` were reassigned on a live instance.
        """
        return ZoneInfo(self.timezone)

    # --- offset queries -----------------------------------------------------

    def utc_offset_at(self, instant):
        """REAL offset at an aware instant. This is what `offset` only pretends to be."""
        return instant.astimezone(self.zone).utcoffset()

    def offset_label_at(self, instant):
        """'UTC+05:30' for the offset actually in force at `instant`."""
        total = int(self.utc_offset_at(instant).total_seconds())
        sign = "+" if total >= 0 else "-"
        hours, minutes = divmod(abs(total) // 60, 60)
        return f"UTC{sign}{hours:02d}:{minutes:02d}"

    def is_dst_at(self, instant):
        return bool(instant.astimezone(self.zone).dst())

    def standard_offset(self, year=None):
        """Non-DST offset — the minimum offset observed across a year.

        Hemisphere-agnostic: DST always ADDS to standard time, so the yearly minimum
        is standard everywhere. Used by the drift audit (§5), never by the engine.
        """
        from django.utils import timezone as dj_tz

        year = year or dj_tz.now().year
        probes = (datetime(year, m, 15, 12, tzinfo=UTC) for m in range(1, 13))
        return min(self.utc_offset_at(p) for p in probes)

    # --- the conversion that matters ----------------------------------------

    def to_utc(self, day, wall_time, *, prefer_fold=0):
        """Naive local (date, time) in THIS zone -> (aware UTC datetime, flag).

        `prefer_fold` selects which occurrence of an AMBIGUOUS local time is meant:
        0 = the first (DST still in force), 1 = the second. For an interval, pass 0
        for the start and 1 for the end so the interval covers real elapsed time.

        `flag` is "" | "nonexistent" | "ambiguous" — persist it on derived rows so a
        surprising duration is explainable from the database.
        """
        aware, flag = _resolve_local(day, wall_time, self.zone, prefer_fold=prefer_fold)
        return aware.astimezone(UTC), flag
```

### 3.4 Behaviour reference

`America/New_York`, all verified:

| Input | Output | flag |
|---|---|---|
| `2026-06-01 09:00` | `2026-06-01 13:00Z` | `""` |
| `2026-03-08 02:30` (doesn't exist) | `2026-03-08 07:00Z` | `nonexistent` |
| `2026-11-01 01:30`, `prefer_fold=0` | `2026-11-01 05:30Z` | `ambiguous` |
| `2026-11-01 01:30`, `prefer_fold=1` | `2026-11-01 06:30Z` | `ambiguous` |
| `utc_offset_at(Jan)` / `(Jul)` | `-05:00` / `-04:00` | — |
| `offset_label_at` — `Pacific/Chatham`, Jan | `UTC+13:45` | — |

---

## 4. Phase 2 — migrate the callers

### 4.1 `core/services/availability.py`

Two sites, mechanical:

```python
-    tz = ZoneInfo(profile.timezone.timezone)
+    tz = profile.timezone.zone
```

([:85](../../core/services/availability.py#L85) and [:281](../../core/services/availability.py#L281).)
The `ZoneInfo("UTC")` uses at [:273-274](../../core/services/availability.py#L273-L274) and
[:296-297](../../core/services/availability.py#L296-L297) are literal UTC, not zone lookups —
replace with a module constant, no behaviour change.

Both call sites are **deleted entirely** by the overnight work. Migrating them now is a few
lines and keeps the "no ad-hoc `ZoneInfo`" rule true in the interim rather than aspirational.

### 4.2 `core/models/campaign.py` — a real policy divergence

[`combine_local()`](../../core/models/campaign.py#L40-L52) documents a **deliberate** and
**different** policy:

> *"Ambiguous and non-existent times (DST transitions) resolve through ZoneInfo's default
> `fold=0` rather than raising — a campaign is not worth failing over a one-hour-a-year edge."*

Reconciliation, stated explicitly because this is a behaviour change:

| Case | Campaigns today | After T4 | Impact |
|---|---|---|---|
| Ambiguous | `fold=0` (first occurrence) | `fold=0` (default) | **none** |
| Nonexistent | gap-**width** shift → `02:30` fires at `03:30` | transition **instant** → fires at `03:00` | fires ≤1 h earlier, once a year, only if scheduled inside the gap |
| Never raises | ✓ | ✓ | **none** — `to_utc` cannot raise for a valid zone |

The campaign case is a **single instant**, so it never hits the zero-length-interval failure
that forced the stricter policy. Both policies are defensible there; adopting one is worth more
than preserving a half-hour difference in an edge that fires once a year. `03:00` is also
closer to the scheduler's intent than `03:30`.

```python
def combine_local(start_date, start_time, tz_name):
    tz_obj = Timezone.objects.filter(timezone=tz_name).first()
    if tz_obj is None:
        tz_obj = Timezone(timezone=tz_name)      # unsaved shim; `zone` still resolves
    utc, _flag = tz_obj.to_utc(start_date, start_time)
    return utc
```

**Keep `resolve_tenant_timezone()` as-is.** Its fallback exists for `Tenant.timezone` being
**null** — a real state, since the field is nullable and its default is a DB lookup returning
`None` on an unseeded database. T2 removes the *invalid-name* case; it does not remove the
*absent* case.

---

## 5. Phase 3 — drift audit command

`onboard/management/commands/audit_timezone_offsets.py`:

```
manage.py audit_timezone_offsets [--fix] [--year=2026]
```

For every `Timezone` row, compares stored `offset` against `standard_offset()` and reports
drift. `--fix` rewrites the column. Exits non-zero when drift is found so CI/cron can gate on it.

**Verified against the seeded data: the audit reports clean.** Every stored `offset` already
equals its zone's standard-time offset — the seed is correct *as standard time*. The 14
DST-observing zones are not "drifted"; they are standard-time values being *displayed* as
though they were current, which is what `current_offset` (§6) fixes rather than this command.

This command's job is therefore **future** drift: `tzdata` moving a zone's *standard* offset,
which does happen (Russia 2014, Samoa 2011, Kazakhstan 2024). Re-run after every `tzdata` bump.

> One caveat worth knowing: a handful of zones are modelled in tzdb with **negative** DST
> (`Europe/Dublin`), where "standard" is summer. `standard_offset()`'s yearly-minimum
> definition still yields the value users expect to see (+00:00 for Dublin), which is what
> a display string is for. None of these zones are in the current seed.

---

## 6. Phase 4 — serializers (T7)

Additive only:

```python
class TimezoneRefSerializer(serializers.ModelSerializer):
    current_offset = serializers.SerializerMethodField()

    class Meta:
        model = Timezone
        fields = ("id", "timezone", "offset", "current_offset")
        read_only_fields = fields

    def get_current_offset(self, obj) -> str:
        return obj.offset_label_at(timezone.now())
```

Same for `TimezoneSerializer` / `TimezoneListSerializer`. `offset` keeps its shape so no client
breaks; `current_offset` is the one to actually display.

`TimezoneDropdownSerializer.get_label` ([:71-72](../../onboard/serializers/timezone.py#L71-L72))
currently renders the static string — switch it to `offset_label_at(now)` so the picker stops
showing London as `UTC+00:00` in July.

**Cost note:** `current_offset` is ~6 µs per row; a 37-row dropdown is immaterial. If a listing
ever grows large, hoist `timezone.now()` into the serializer context rather than calling it
per row.

---

## 7. Phase 5 — tests

`onboard/tests/test_timezone_conversion.py`:

| Test | Asserts |
|---|---|
| `test_utc_offset_at_tracks_dst` | `America/New_York` → −05:00 Jan, −04:00 Jul |
| `test_offset_label_at_formats` | `Asia/Kolkata` → `UTC+05:30`; `Pacific/Chatham` Jan → `UTC+13:45`; negative offsets render `UTC-05:00` |
| `test_is_dst_at` | `Europe/London` true in Jul, false in Jan |
| `test_standard_offset_both_hemispheres` | NY → −05:00; `Australia/Sydney` → +10:00 |
| `test_to_utc_ordinary` | flag `""`, correct instant |
| `test_to_utc_nonexistent_snaps_to_transition` | `2026-03-08 02:30` NY → `07:00Z`, flag `nonexistent` |
| `test_to_utc_gap_interval_not_zero_length` | **regression.** `02:30`→`03:30` on 2026-03-08 → 30 min apart. Gap-width shift gives 0 |
| `test_to_utc_ambiguous_fold` | `2026-11-01 01:30` → `05:30Z` (fold 0) / `06:30Z` (fold 1), flag `ambiguous` |
| `test_invalid_timezone_rejected` | `full_clean()` on `"Mars/Olympus"` raises `ValidationError` |
| `test_every_seeded_zone_constructs` | all 37 rows in `json_files/timezone.json` resolve — catches a bad seed **and** missing `tzdata` (T6) in CI |
| `test_offset_column_not_used_for_arithmetic` | mutating `obj.offset` does not change `to_utc()` output |

`core/tests/campaigns/` — add `test_combine_local_nonexistent_uses_transition_instant`
documenting the §4.2 change.

---

## 8. Deploy

```
1. Pin tzdata in pyproject.toml, lock, rebuild image   # T6
2. manage.py migrate                                    # AlterField only — no SQL change
3. manage.py audit_timezone_offsets                     # expect CLEAN on correct seed data
4. Spot-check the dropdown renders London as UTC+01:00 in summer
```

Add to the **dependency-bump** runbook, not just the deploy runbook: a lockfile refresh can move
`tzdata` without anyone intending a timezone change, so `audit_timezone_offsets` should run there
too.

---

## 9. What this deliberately does not do

**Live patching without a deploy.** If a government changes rules on short notice and the pinned
`tzdata` predates it, those users are wrong until a release ships. The interim answer is
`pip install -U tzdata` — a dependency bump, not a code change.

The follow-up, if and when it is needed, is a `TimezoneOverride` table checked ahead of
`zoneinfo` inside `utc_offset_at()` / `to_utc()` — roughly 20 lines plus a model, layering
cleanly on top of everything here. It needs an exclusion constraint on overlapping ranges, a
rematerialization trigger, and a staleness report. **Not built now:** it has no consumer until a
real zone needs it, and carrying an untested escape hatch through the overnight work adds risk
for no benefit.

---

## 10. Estimate

| Phase | Scope |
|---|---|
| 1 — model + validator + helpers | S — the algorithm is already verified; this is transcription |
| 2 — caller migration | S — 2 mechanical sites + 1 campaign policy change with a test |
| 3 — audit command | S |
| 4 — serializers | S |
| 5 — tests | M — 12 tests, DST cases need fixed-date fixtures |

Lands standalone. The overnight work then builds on `to_utc()` instead of reimplementing it.
