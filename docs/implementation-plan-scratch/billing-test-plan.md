# Billing, Credits & Entitlements — Test Specification

Every test case for everything built in this cycle. **Specification only — no test code
exists yet.** Review this, tell me what to add, remove or change, and then I will
implement it.

## How to read this

| Column | Meaning |
| :--- | :--- |
| **ID** | Stable reference. Use it to tell me "drop EN-14" or "add a case after WS-22" |
| **Scenario** | The situation under test |
| **Expected** | What must be true afterwards |

**P0** cases protect money or data integrity — a failure means a tenant is charged
wrongly, charged twice, or not charged at all. **P1** is correctness that is visible to
a user. **P2** is polish.

Every suite runs with `--settings=config.settings.test_postgres`. SQLite cannot host
this codebase's `ArrayField` or the partial unique constraints these tests depend on.

> **Renewal and reactivation rules these cases pin down**, settled in review:
>
> | State | Renew same plan | Switch plan | Top-up / add-ons | Read own state |
> | :--- | :--- | :--- | :--- | :--- |
> | Active, >14 days left | refused — window not open | prorated | yes | yes |
> | Active, ≤14 days left | yes | prorated | yes | yes |
> | **Expired** | yes, **extends the same row** | yes, **full price** | **no** | **yes** |
>
> An expired tenant can come back on *any* plan in a single payment. Before this,
> returning on a bigger plan meant renewing the old one and then upgrading — two
> payments for one outcome.

## Proposed file layout

| File | Cases |
| :--- | ---: |
| `wallet/tests/test_models.py` | 24 |
| `wallet/tests/test_ledger_immutability.py` | 15 |
| `wallet/tests/test_rating.py` | 26 |
| `wallet/tests/test_policy.py` | 9 |
| `wallet/tests/test_wallet_service.py` | 55 |
| `wallet/tests/test_wallet_api.py` | 30 |
| `wallet/tests/test_wallet_tasks.py` | 16 |
| `onboard/tests/test_subscription_model.py` | 18 |
| `onboard/tests/test_subscription_lifecycle.py` | 55 |
| `onboard/tests/test_pricing.py` | 50 |
| `onboard/tests/test_entitlements.py` | 36 |
| `onboard/tests/test_billing_api_purchases.py` | 30 |
| `onboard/tests/test_billing_api_subscription.py` | 52 |
| `onboard/tests/test_billing_webhook.py` | 28 |
| `onboard/tests/test_billing_tasks.py` | 22 |
| `onboard/tests/test_payment_settings.py` | 14 |
| `onboard/tests/test_billing_commands.py` | 18 |
| `onboard/tests/test_billing_notifications.py` | 15 |
| `core/tests/billing/test_credit_consumption.py` | 22 |
| `core/tests/billing/test_entitlement_guards.py` | 24 |
| `onboard/tests/test_billing_journeys.py` (end-to-end) | 16 |
| `onboard/tests/test_razorpay_live.py` *(opt-in)* | 12 |
| `core/tests/billing/test_push_live.py` *(opt-in)* | 9 |
| `wallet/tests/test_admin_actions.py` + `onboard/…` | 26 |
| `wallet/tests/test_query_budget.py` | 14 |
| `onboard/tests/test_pricing_yearly.py` | 16 |
| `wallet/tests/test_concurrency.py` | 12 |
| `onboard/tests/test_migrations.py` | 20 |
| *(currency, folded into pricing)* | 4 |
| **Total** | **688** |

---

# 1. Wallet models — `wallet/tests/test_models.py`

## 1.1 `Wallet`

| ID | Scenario | Expected | P |
| :--- | :--- | :--- | :--- |
| WM-01 | `Wallet.for_tenant()` on a tenant with no wallet | Creates and returns one, balance 0 | P0 |
| WM-02 | `for_tenant()` called twice | Same row both times, no duplicate | P0 |
| WM-03 | Second wallet for the same tenant | `IntegrityError` — `uniq_wallet_per_tenant` | P0 |
| WM-04 | Wallet for a tenant whose previous wallet is soft-deleted | Allowed; the partial constraint ignores deleted rows | P1 |
| WM-05 | `balance != allocated + purchased` | `IntegrityError` — `ck_wallet_balance_is_sum_of_buckets` | P0 |
| WM-06 | `balance == allocated + purchased` | Accepted | P0 |
| WM-07 | Negative `balance` | Rejected (PositiveIntegerField) | P0 |
| WM-08 | `is_spendable` — not frozen, balance > 0 | `True` | P1 |
| WM-09 | `is_spendable` — frozen, balance > 0 | `False` | P1 |
| WM-10 | `is_spendable` — not frozen, balance 0 | `False` | P1 |
| WM-11 | `is_low` — threshold 10, balance 5 | `True` | P1 |
| WM-12 | `is_low` — threshold 10, balance 10 | `True` (at, not just below) | P1 |
| WM-13 | `is_low` — threshold 0 (disabled) | `False` at any balance | P1 |
| WM-14 | `__str__` | Contains tenant and balance | P2 |

## 1.2 `CreditRate`

| ID | Scenario | Expected | P |
| :--- | :--- | :--- | :--- |
| WM-15 | Two open-ended rates, same plan + operation | `IntegrityError` — `uniq_current_rate_per_plan_operation` | P0 |
| WM-16 | One superseded + one open-ended, same plan + operation | Allowed | P0 |
| WM-17 | Same operation on two different plans, both open | Allowed | P0 |
| WM-18 | `effective_to == effective_from` | `IntegrityError` — `ck_credit_rate_period_valid` | P1 |
| WM-19 | `effective_to < effective_from` | `IntegrityError` | P1 |
| WM-20 | `effective_to` null | Allowed; `is_current` is `True` | P1 |
| WM-21 | Soft-deleted open-ended rate frees the slot | A new open-ended rate can be created | P1 |
| WM-22 | `unit_divisor` for `per_minute` / `per_second` / `per_event` | 60 / 1 / `None` | P0 |
| WM-23 | `as_snapshot_entry()` shape | Keys: unit, credits_per_unit, rounding, minimum_units, rate_id | P0 |
| WM-24 | `plan` is required | Cannot create a rate with no plan | P1 |

---

# 2. Ledger immutability — `wallet/tests/test_ledger_immutability.py`

The balance invariant is only meaningful if these hold.

| ID | Scenario | Expected | P |
| :--- | :--- | :--- | :--- |
| LI-01 | `save()` on a persisted ledger row | `NotImplementedError` | P0 |
| LI-02 | `delete()` on an instance | `NotImplementedError` | P0 |
| LI-03 | `soft_delete()` on an instance | `NotImplementedError` | P0 |
| LI-04 | `restore()` on an instance | Raises (routes through `save`) | P1 |
| LI-05 | `deactivate()` on an instance | Raises (routes through `save`) | P1 |
| LI-06 | `queryset.delete()` | `NotImplementedError` | P0 |
| LI-07 | `queryset.soft_delete()` | `NotImplementedError` — **the one that would silently break sums** | P0 |
| LI-08 | `queryset.update()` | `NotImplementedError` | P0 |
| LI-09 | Same on `all_objects` | Blocked identically | P0 |
| LI-10 | Creating a new row | Allowed | P0 |
| LI-11 | Admin exposes no add/change/delete for `CreditLedger` | All three permissions `False`, `actions is None` | P1 |

## 2.1 Ledger constraints

| ID | Scenario | Expected | P |
| :--- | :--- | :--- | :--- |
| LI-12 | `amount = 0` | `IntegrityError` — `ck_ledger_amount_non_zero` | P0 |
| LI-13 | Duplicate `(entry_type, reference_id)` | `IntegrityError` — `uniq_ledger_entry_reference` | P0 |
| LI-14 | Same `reference_id`, different `entry_type` | Allowed | P0 |
| LI-15 | Multiple rows with `reference_id = NULL` | Allowed (Postgres permits many NULLs) | P0 |

---

# 3. Rating engine — `wallet/tests/test_rating.py`

## 3.1 `rate_quantity` arithmetic

| ID | Scenario | Expected | P |
| :--- | :--- | :--- | :--- |
| WR-01 | 90s at 5/min, ceil, min 1 | 2 units → **10 credits** | P0 |
| WR-02 | 60s at 5/min | 1 unit → 5 credits | P0 |
| WR-03 | 61s at 5/min | 2 units → 10 credits (ceil boundary) | P0 |
| WR-04 | 180s at 5/min | 3 units → 15 credits (exact multiple) | P0 |
| WR-05 | 1s at 5/min, min 1 | 1 unit → 5 credits | P0 |
| WR-06 | 0s | Handled without error; see WP-04 for the call path | P1 |
| WR-07 | 90s, rounding `floor` | 1 unit → 5 credits | P1 |
| WR-08 | 90s, rounding `nearest` | 2 units (1.5 rounds half-up) | P1 |
| WR-09 | 89s, rounding `nearest` | 1 unit | P1 |
| WR-10 | `minimum_units = 2`, 30s at per_minute | 2 units, not 1 | P1 |
| WR-11 | `per_event` unit, any duration | 1 unit; duration ignored | P1 |
| WR-12 | `per_event` with `minimum_units = 3` | 3 units | P2 |
| WR-13 | `per_second` unit, 90s at 1/sec | 90 units → 90 credits | P1 |
| WR-14 | Negative quantity | Clamped to 0 | P1 |
| WR-15 | `credits_per_unit = 0` | 0 credits, no error (a free tier) | P1 |
| WR-16 | Result carries `raw_quantity`, `units`, `unit_type`, `rate_applied`, `operation`, `rate_id` | All populated | P0 |

## 3.2 `resolve_rate`

| ID | Scenario | Expected | P |
| :--- | :--- | :--- | :--- |
| WR-17 | Wallet has a stamp containing the operation | Returns it; `source == "snapshot"`; **no query** | P0 |
| WR-18 | Wallet has no stamp but `current_plan` has a live rate | Returns it; `source == "live_plan"`; logs a warning | P1 |
| WR-19 | No stamp, no `current_plan` | `RateNotConfigured` | P0 |
| WR-20 | Stamp exists but lacks the requested operation | Falls back, then raises if unavailable | P0 |
| WR-21 | Stamp missing `rounding` / `minimum_units` keys | Defaults to `ceil` / 1 | P1 |

## 3.3 `build_snapshot` / `stamp`

| ID | Scenario | Expected | P |
| :--- | :--- | :--- | :--- |
| WR-22 | Snapshot for a plan with 3 current rates | 3 operations, plan_id, plan_code, stamped_at | P0 |
| WR-23 | Superseded rates excluded | Only open-ended rates appear | P0 |
| WR-24 | Two open rates for one operation (defensive) | Newest `effective_from` wins | P2 |
| WR-25 | `stamp()` sets `rate_snapshot`, `rate_snapshot_at`, `current_plan` | All three written | P0 |
| WR-26 | Re-stamping to another plan | Snapshot replaced; the *next* rating uses new prices, earlier ledger rows unchanged | P0 |

---

# 4. Consumption policy — `wallet/tests/test_policy.py`

| ID | Scenario | Expected | P |
| :--- | :--- | :--- | :--- |
| WP-01 | Call log has `service_tier`, agent has a different one | **Call log wins** | P0 |
| WP-02 | Call log `service_tier` is null, agent has one | Agent's tier used | P0 |
| WP-03 | Neither has one | `DEFAULT_TIER` (standard, not realtime — never overcharge for a misconfiguration) | P1 |
| WP-04 | `duration_seconds = 0` | `operation_for_call` returns `None` | P0 |
| WP-05 | `duration_seconds` is null | Returns `None` | P0 |
| WP-06 | `duration_seconds = 90` | `(tier, 90)` | P0 |
| WP-07 | Call log with no `agent` | Falls to `DEFAULT_TIER`, does not raise | P1 |
| WP-08 | Every `ServiceTier` value is a valid `CreditRate.Operation` | Sets are equal — the drift guard | P0 |
| WP-09 | `Agent.service_tier` default | `conversation_high_latency` | P2 |

---

# 5. Wallet service — `wallet/tests/test_wallet_service.py`

## 5.1 `grant`

| ID | Scenario | Expected | P |
| :--- | :--- | :--- | :--- |
| WS-01 | Grant 1000 to allocated | balance 1000, allocated 1000, purchased 0 | P0 |
| WS-02 | Grant 500 to purchased | purchased 500, allocated unchanged | P0 |
| WS-03 | Ledger row written with correct `balance_after` | Matches wallet balance after | P0 |
| WS-04 | `lifetime_granted` increases | By the granted amount | P1 |
| WS-05 | Replayed `reference_id` | Returns `None`, **no second row, balance unchanged** | P0 |
| WS-06 | Grant with `reference_id=None` twice | Both land (no dedup possible without a key) | P1 |
| WS-07 | Grant of 0 | `ValueError` | P1 |
| WS-08 | Grant of a negative amount | `ValueError` | P1 |
| WS-09 | **Grant on a frozen wallet** | Succeeds — a renewal payment must be able to land | P0 |
| WS-10 | `subscription` and `performed_by` recorded on the row | Both set | P1 |

## 5.2 `consume`

| ID | Scenario | Expected | P |
| :--- | :--- | :--- | :--- |
| WS-11 | 90s low-latency at 10/min with 1000 balance | −20, balance 980 | P0 |
| WS-12 | Rating snapshot on the row | `raw_quantity` 90, `units` 2, `unit_type` per_minute, `rate_applied` 10 | P0 |
| WS-13 | Replayed `reference_id` | `None`, nothing written | P0 |
| WS-14 | Balance exactly equals the cost | Succeeds, balance 0 | P0 |
| WS-15 | Balance one credit short | Returns `None`, logs, **writes nothing** | P0 |
| WS-16 | Zero-credit result (rate 0) | Returns `None`, no zero-amount row | P0 |
| WS-17 | `lifetime_consumed` increases | By the charged amount | P1 |
| WS-18 | Unpriced operation | `RateNotConfigured` | P0 |
| WS-19 | `metadata` recorded | Present on the row | P2 |

## 5.3 Bucket arithmetic

| ID | Scenario | Expected | P |
| :--- | :--- | :--- | :--- |
| WS-20 | 980 allocated + 50 purchased, charge 20 | Drains **allocated first**: 960/50 | P0 |
| WS-21 | 5 allocated + 50 purchased, charge 15 | **Two rows**: allocated −5, purchased −10 | P0 |
| WS-22 | Second row's `reference_id` | `{ref}#purchased` | P0 |
| WS-23 | Replay of a cross-bucket charge | Nothing written (probe on the primary ref covers both) | P0 |
| WS-24 | Per-bucket invariant after a split charge | `SUM(allocated rows) == allocated_balance`, same for purchased | P0 |
| WS-25 | 0 allocated + 50 purchased, charge 20 | Single row from purchased | P0 |

## 5.4 `refund`

| ID | Scenario | Expected | P |
| :--- | :--- | :--- | :--- |
| WS-26 | Refund a single-bucket charge | Credits restored to the same bucket | P0 |
| WS-27 | Refund a cross-bucket charge | **Each bucket restored exactly as drawn** (5/10, not 15 to one) | P0 |
| WS-28 | Replayed refund | `None`, no double credit | P0 |
| WS-29 | Refund an unknown reference | `None`, logs a warning | P1 |
| WS-30 | `lifetime_consumed` reduced, floored at 0 | Never negative | P1 |

## 5.5 `forfeit`

| ID | Scenario | Expected | P |
| :--- | :--- | :--- | :--- |
| WS-31 | Forfeit a specific amount | That amount removed from the named bucket | P0 |
| WS-32 | `amount=None` | Whole bucket burned | P0 |
| WS-33 | Amount greater than held | Clamped to what is held; never negative | P0 |
| WS-34 | Empty bucket | Returns `None`, no zero-amount row | P0 |
| WS-35 | **`lifetime_consumed` unchanged** | A forfeit is not consumption | P1 |
| WS-36 | Replayed forfeit reference | `None` | P0 |

## 5.6 `adjust`

| ID | Scenario | Expected | P |
| :--- | :--- | :--- | :--- |
| WS-37 | Positive adjustment | Credits added; **defaults to the purchased bucket** | P0 |
| WS-38 | Negative adjustment | Credits removed, allocated first | P0 |
| WS-39 | Adjustment of 0 | `ValueError` | P1 |
| WS-40 | `performed_by` recorded | Set on the row | P1 |
| WS-41 | Replayed adjustment reference | `None` | P0 |

## 5.7 Guards, state and concurrency

| ID | Scenario | Expected | P |
| :--- | :--- | :--- | :--- |
| WS-42 | `assert_can_consume`, sufficient balance | Returns, no exception | P0 |
| WS-43 | Short balance, `credits_enforced=False` | Logs, **does not raise** | P0 |
| WS-44 | Short balance, `credits_enforced=True` | `InsufficientCredits` with `required` / `available` | P0 |
| WS-45 | Frozen wallet, enforcement off | Logs, does not raise | P0 |
| WS-46 | Frozen wallet, enforcement on | `WalletFrozen` | P0 |
| WS-47 | `_enforced()` reads without creating a `TenantSettings` row | No row created on the hot path | P1 |
| WS-48 | `freeze()` sets reason and timestamp | All three fields set | P1 |
| WS-49 | `freeze()` on an already-frozen wallet | No-op, reason preserved | P2 |
| WS-50 | `unfreeze()` clears reason and timestamp | Both null | P1 |
| WS-51 | `set_plan_context()` stamps rates **and** copies `low_balance_threshold` | Both set from the plan | P0 |
| WS-52 | **Two concurrent `consume` calls on a one-credit wallet** | Exactly one succeeds; balance never negative | P0 |
| WS-53 | Invariant after a randomised sequence of 50 mixed operations | `SUM(ledger) == balance`, per bucket | P0 |
| WS-54 | `balance_for()` shape | balance, allocated, purchased, is_frozen, is_low, plan_code | P1 |
| WS-55 | `quote()` writes nothing | Ledger count unchanged | P0 |

---

# 6. Wallet API — `wallet/tests/test_wallet_api.py`

## 6.1 `GET /billing/wallet/`

| ID | Scenario | Expected | P |
| :--- | :--- | :--- | :--- |
| WA-01 | Authenticated tenant | 200, correct balance and buckets | P0 |
| WA-02 | Anonymous | 401/403 | P0 |
| WA-03 | Tenant B cannot see tenant A's balance | Each sees only its own | P0 |
| WA-04 | User with no tenant mapping | 403 | P1 |
| WA-05 | Frozen wallet | `is_frozen` true with reason, `is_spendable` false | P1 |
| WA-06 | `plan_code` from the stamp | Matches the subscribed plan | P1 |

## 6.2 `GET /billing/wallet/rates/`

| ID | Scenario | Expected | P |
| :--- | :--- | :--- | :--- |
| WA-07 | Stamped wallet | 3 operations with labels and prices | P0 |
| WA-08 | Prices match the **stamp**, not the live catalogue | Changing the plan's rate does not change this response | P0 |
| WA-09 | Unstamped wallet | 200, empty operations, message says so | P1 |
| WA-10 | Anonymous | 401/403 | P0 |

## 6.3 `POST /billing/wallet/quote/`

| ID | Scenario | Expected | P |
| :--- | :--- | :--- | :--- |
| WA-11 | Valid operation and quantity | 200 with credits, units, rate_applied | P0 |
| WA-12 | **Quote equals what `consume` later charges** | Same number | P0 |
| WA-13 | `quantity = 0` | 400 | P1 |
| WA-14 | Negative quantity | 400 | P1 |
| WA-15 | Missing `operation` | 400 | P1 |
| WA-16 | Unknown operation string | 400 | P1 |
| WA-17 | Operation with no rate on this plan | **409 `RATE_NOT_CONFIGURED`**, not 500 | P0 |
| WA-18 | `affordable` false when balance is short | Reported, request still 200 | P1 |
| WA-19 | Frozen wallet | `affordable` false | P1 |
| WA-20 | No ledger row written | Count unchanged | P0 |

## 6.4 `GET /billing/wallet/ledger/`

| ID | Scenario | Expected | P |
| :--- | :--- | :--- | :--- |
| WA-21 | Lists this tenant's rows, newest first | Correct order | P0 |
| WA-22 | **Tenant B's rows never appear** | Strict isolation | P0 |
| WA-23 | Usage row includes nested `rating` | 4 keys populated | P0 |
| WA-24 | Non-usage row | `rating` is `null` | P1 |
| WA-25 | Filter `entry_type` | Only matching rows | P1 |
| WA-26 | Filter `bucket` | Only matching rows | P1 |
| WA-27 | Filter `is_credit=true` / `false` | Positive / negative amounts | P1 |
| WA-28 | Filter `created_after` / `created_before` | Window respected | P1 |
| WA-29 | Pagination shape | Rows in `data`, pagination in `meta` — **not `data.results`** | P1 |
| WA-30 | `POST` / `PUT` / `PATCH` / `DELETE` | **405 on all four** | P0 |

---

# 7. Wallet tasks — `wallet/tests/test_wallet_tasks.py`

| ID | Scenario | Expected | P |
| :--- | :--- | :--- | :--- |
| WT-01 | `report_unrated_calls` — a call with duration and no deduction | Reported, count 1 | P0 |
| WT-02 | A charged call | Not reported | P0 |
| WT-03 | A zero-duration call | Not reported | P1 |
| WT-04 | A call inside the 15-minute grace window | Not reported | P1 |
| WT-05 | A call older than the lookback window | Not reported | P2 |
| WT-06 | Reports only — no charge is made | Ledger unchanged | P0 |
| WT-07 | `reconcile_wallets` — all wallets agree | Returns 0 | P0 |
| WT-08 | Injected drift | Non-zero, drift logged with wallet id | P0 |
| WT-09 | Per-bucket drift only | Detected | P0 |
| WT-10 | **Default run never repairs** | Balance untouched | P0 |
| WT-11 | `send_low_balance_alerts` at/below threshold | Notification created | P1 |
| WT-12 | Above threshold | Nothing sent | P1 |
| WT-13 | Threshold 0 | Nothing sent | P1 |
| WT-14 | Re-run inside the quiet window | Not re-sent | P1 |
| WT-15 | Frozen wallet | Skipped | P2 |
| WT-16 | Task names are stable (`wallet.tasks.*`) | Match the beat schedule entries | P1 |

---

# 8. Subscription model — `onboard/tests/test_subscription_model.py`

| ID | Scenario | Expected | P |
| :--- | :--- | :--- | :--- |
| SM-01 | Create with no `end_date`, monthly plan | Derived as start + 1 calendar month | P0 |
| SM-02 | Yearly plan | start + 1 year | P0 |
| SM-03 | **Start 31 Jan, monthly** | End 28/29 Feb — calendar, not 30 days | P0 |
| SM-04 | Existing `end_date` on save | **Never recomputed** (a renewal must not be undone) | P0 |
| SM-05 | `end_date` is non-nullable at the DB level | Insert with NULL fails | P0 |
| SM-06 | Second ACTIVE subscription for one tenant | `IntegrityError` — `uniq_active_subscription_per_tenant` | P0 |
| SM-07 | One ACTIVE + one CANCELLED | Allowed | P0 |
| SM-08 | One ACTIVE + one soft-deleted ACTIVE | Allowed | P1 |
| SM-09 | `plan_snapshot` generated on first save | Money + credit rules + entitlements | P0 |
| SM-10 | Snapshot contains all 9 credit-rule keys | Present | P0 |
| SM-11 | Snapshot contains an `entitlements` map | One entry per resource | P0 |
| SM-12 | `rules` reads from the snapshot | Editing the live plan does not change it | P0 |
| SM-13 | `rules` falls back to the live plan for a missing key | Value returned | P1 |
| SM-14 | `included_entitlement()` from the snapshot | Correct per resource | P0 |
| SM-15 | `included_entitlement()` for an unknown resource | `None` | P1 |
| SM-16 | `cycle_delta()` for an unrecognised cycle | Falls back to monthly | P1 |
| SM-17 | `is_expired` before / after `end_date` | False / True | P1 |
| SM-18 | `has_scheduled_change` | True only when `scheduled_plan` is set | P1 |

---

# 9. Subscription lifecycle — `onboard/tests/test_subscription_lifecycle.py`

## 9.1 `activate`

| ID | Scenario | Expected | P |
| :--- | :--- | :--- | :--- |
| SL-01 | Fresh tenant on a paid plan | Subscription ACTIVE with `end_date` | P0 |
| SL-02 | Wallet stamped with the plan's rate card | 3 operations present | P0 |
| SL-03 | Cycle credits granted | Balance equals `allocated_credits` | P0 |
| SL-04 | `low_balance_threshold` copied | From the plan | P1 |
| SL-05 | Grant references the payment order | `reference_id == razorpay_order_id` | P0 |
| SL-06 | Ledger row links the subscription | `subscription_id` set | P1 |
| SL-07 | **Stamp happens before the grant** | A call between the two would price on the new plan | P0 |
| SL-08 | Called twice for the same plan | Same subscription returned; **credits granted once** | P0 |
| SL-09 | Called with no transaction | Reference is `subscription:{pk}`; still idempotent | P1 |
| SL-10 | Called for a *different* plan | Old closed, new created, `superseded_by` set | P0 |
| SL-11 | Plan with `allocated_credits = 0` | No grant, no zero-amount row | P1 |
| SL-12 | Wallet failure during stamping | Subscription still exists; error logged; no rollback | P1 |
| SL-13 | Free plan | Trial credits granted, rates stamped | P0 |

## 9.2 `rollover_amount`

| ID | Scenario | Expected | P |
| :--- | :--- | :--- | :--- |
| SL-14 | Flag off | `(0, held, "rollover_forfeit")` | P0 |
| SL-15 | Flag on, under cap | `(held, 0, None)` | P0 |
| SL-16 | Flag on, over cap | `(cap, held - cap, "rollover_cap_forfeit")` | P0 |
| SL-17 | Flag on, cap null | Everything carries | P0 |
| SL-18 | Reads the **outgoing** subscription's snapshot | Not the incoming plan's rules | P0 |
| SL-19 | Purchased credits never counted | Only the allocated bucket is at stake | P0 |
| SL-20 | Each reason maps to its own flag | renewal / upgrade / downgrade | P0 |

## 9.3 `renew`

| ID | Scenario | Expected | P |
| :--- | :--- | :--- | :--- |
| SL-21 | Renewal extends the **same row** | No second subscription created | P0 |
| SL-22 | `end_date` moves one cycle forward | Correct new date | P0 |
| SL-23 | Plan with no renewal rollover | Old allocated credits forfeited, fresh allocation granted | P0 |
| SL-24 | Plan **with** renewal rollover | Old + new allocated | P0 |
| SL-25 | Rollover cap on renewal | Clamped, `rollover_cap_forfeit` written | P0 |
| SL-26 | **Purchased credits survive every renewal** | Untouched | P0 |
| SL-27 | Snapshot re-frozen from the live plan | A plan edit reaches the tenant at renewal | P0 |
| SL-28 | Rate card re-stamped | Current prices | P0 |
| SL-29 | Renewing an EXPIRED subscription | Status ACTIVE, wallet unfrozen | P0 |
| SL-30 | Renewal long after expiry | Starts **now**, not back-dated | P1 |
| SL-31 | Forfeit happens before the new allocation | New credits are never eaten by the forfeit | P0 |

## 9.4 Plan changes

| ID | Scenario | Expected | P |
| :--- | :--- | :--- | :--- |
| SL-32 | `apply_plan_change` upgrade | Old closed, new active, `superseded_by` linked | P0 |
| SL-33 | Wallet re-stamped to the new plan | Next call priced at the new rate | P0 |
| SL-34 | Earlier ledger rows keep their original `rate_applied` | History unchanged | P0 |
| SL-35 | Rollover applied per the **old** plan's rules | Correct carry/forfeit | P0 |
| SL-36 | `schedule_plan_change` | `scheduled_plan` + `scheduled_change_at == end_date`; **plan unchanged** | P0 |
| SL-37 | No second subscription while scheduled | Still exactly one active | P0 |
| SL-38 | `cancel_scheduled_change` | Both fields cleared | P0 |
| SL-39 | Cancel when nothing is scheduled | No-op, no error | P1 |

## 9.5 `expire`

| ID | Scenario | Expected | P |
| :--- | :--- | :--- | :--- |
| SL-40 | Status becomes EXPIRED | Set | P0 |
| SL-41 | Allocated credits forfeited | `expiry_forfeit` row | P0 |
| SL-42 | **Purchased credits kept, not confiscated** | Balance preserved | P0 |
| SL-43 | Wallet frozen with reason | `subscription_expired` | P0 |
| SL-44 | `allow_use_after_expiry = True` | **No freeze, no forfeit** | P0 |
| SL-45 | Expiry then renewal | Purchased credits spendable again | P0 |

## 9.6 `current_or_expired` — the resolver reactivation depends on

| ID | Scenario | Expected | P |
| :--- | :--- | :--- | :--- |
| SL-46 | Tenant has an ACTIVE subscription | Returns it | P0 |
| SL-47 | Tenant has only an EXPIRED one | Returns it — the relationship did not end with the cycle | P0 |
| SL-48 | Tenant has several EXPIRED ones | Returns the most recent by `end_date` | P0 |
| SL-49 | Tenant has only CANCELLED ones | **Returns `None`** — those were superseded by a plan change; the row that replaced them is the one that matters | P0 |
| SL-50 | Tenant with ACTIVE **and** older EXPIRED rows | Returns the active one, never the stale one | P0 |
| SL-51 | Tenant with no subscription at all | Returns `None` | P1 |
| SL-52 | `apply_plan_change` from an EXPIRED subscription | Old row CANCELLED with `superseded_by`; new row ACTIVE | P0 |
| SL-53 | Reactivation grants the new plan's allocation | Balance correct | P0 |
| SL-54 | Reactivation unfreezes the wallet | Spending restored | P0 |
| SL-55 | **Purchased credits survive the whole lapse-and-return journey** | Untouched throughout | P0 |

---

# 10. Pricing — `onboard/tests/test_pricing.py`

## 10.1 `unused_ratio`

| ID | Scenario | Expected | P |
| :--- | :--- | :--- | :--- |
| PR-01 | Day 0 of 30 | ≈ 1.0 | P0 |
| PR-02 | Day 20 of 30 | ≈ 0.3333 | P0 |
| PR-03 | Past `end_date` | 0 | P0 |
| PR-04 | `end_date == start_date` | 0, no division by zero | P1 |

## 10.2 `quote_top_up`

| ID | Scenario | Expected | P |
| :--- | :--- | :--- | :--- |
| PR-05 | 500 credits at ₹1.20 | net 600.00 | P0 |
| PR-06 | 18% GST applied | tax 108.00, total 708.00 | P0 |
| PR-07 | **Not prorated** | Same price on day 1 and day 29 | P0 |
| PR-08 | `cost_per_additional_credit = 0` | `PricingError` | P0 |
| PR-09 | 0 credits | `PricingError` | P1 |
| PR-10 | Negative credits | `PricingError` | P1 |
| PR-11 | Above `MAX_TOP_UP_CREDITS` | `PricingError` | P1 |
| PR-12 | Rounding to 2 decimal places | ROUND_HALF_UP | P1 |
| PR-13 | Breakdown carries credits and unit price | Present | P1 |

## 10.3 `quote_addon`

| ID | Scenario | Expected | P |
| :--- | :--- | :--- | :--- |
| PR-14 | 20 GB on a 10 GB step at ₹199 | 2 blocks → 398.00 recurring | P0 |
| PR-15 | 12 GB on a 10 GB step | **2 blocks** (rounded up) | P0 |
| PR-16 | Prorated at ratio 0.3333 | net ≈ 132.65 | P0 |
| PR-17 | `recurring_net` differs from `total` | Both reported | P0 |
| PR-18 | **Buying 10 then 10 costs the same as 20 at once** | Delta pricing cannot be gamed | P0 |
| PR-19 | Beyond `max_additional` | `PricingError` naming the ceiling | P0 |
| PR-20 | Existing extras counted toward the ceiling | 80 held + 20 more on a 90 cap fails | P0 |
| PR-21 | `is_addon_available = False` | `PricingError` | P0 |
| PR-22 | Resource with no `PlanEntitlement` row | `PricingError` | P1 |
| PR-23 | 0 or negative quantity | `PricingError` | P1 |
| PR-24 | Prices from the **live catalogue**, not the snapshot | An add-on is a new agreement today | P1 |

## 10.4 `quote_plan_change`

| ID | Scenario | Expected | P |
| :--- | :--- | :--- | :--- |
| PR-25 | Starter → Professional at ratio 0.3333 | credit 332.97, taxable 2366.13, total 2792.03 | P0 |
| PR-26 | Change type `paid_change` when money is owed | Correct | P0 |
| PR-27 | `scheduled_change` when nothing is owed | total 0.00 | P0 |
| PR-28 | `immediate_free` when total < ₹1 | Correct branch | P0 |
| PR-29 | **Tax at the new plan's rate**, on the taxable difference | Not the old plan's | P0 |
| PR-30 | Old price from the **snapshot** | Editing the live plan does not change the credit | P0 |
| PR-31 | Add-on recurring credited into the proration | Reduces the amount due | P1 |
| PR-32 | Same plan | `PricingError` | P1 |
| PR-33 | Monthly → yearly | Priced by sign, not by tier | P1 |
| PR-34 | Yearly → monthly | Scheduled, not refunded | P1 |
| PR-35 | Same-price sidegrade | Scheduled (nothing owed) | P2 |
| PR-36 | `effective_at` — now for paid, `end_date` for scheduled | Correct | P1 |

## 10.5 `credit_impact`

| ID | Scenario | Expected | P |
| :--- | :--- | :--- | :--- |
| PR-37 | Rollover allowed | `carried_forward` = allocated, `forfeited` 0, no warnings | P0 |
| PR-38 | Rollover not allowed | Warning text names the number of credits | P0 |
| PR-39 | Cap exceeded | Warning says the cap was hit | P0 |
| PR-40 | `balance_after_change` arithmetic | carried + purchased + new allocation | P0 |

## 10.6 Reactivation — quoting a lapsed subscription

Once a cycle is over there is no unused time to credit and no cycle to schedule
against, so "upgrade" and "downgrade" stop being meaningful. Every option becomes a
full-price purchase.

| ID | Scenario | Expected | P |
| :--- | :--- | :--- | :--- |
| PR-41 | Quote for an EXPIRED subscription | `reactivation: true` in the breakdown | P0 |
| PR-42 | Credit for unused time | **0.00** — the cycle is over | P0 |
| PR-43 | Amount due | The **full** price of the chosen plan | P0 |
| PR-44 | Expired, moving to a **cheaper** plan | Still `paid_change` — never scheduled, because there is no cycle to schedule into | P0 |
| PR-45 | Expired, moving to the **free** plan | `immediate_free`, **not** `scheduled_change` with a past `effective_at` | P0 |
| PR-46 | `effective_at` on a reactivation | Now, never the lapsed `end_date` | P0 |
| PR-47 | Subscription past `end_date` but still marked ACTIVE | Treated as reactivating — the date decides, not just the status | P0 |
| PR-48 | Add-on credit on a reactivation | 0.00; nothing is credited from a dead cycle | P1 |
| PR-49 | **An ACTIVE tenant is unaffected** | A downgrade is still `scheduled_change` at 0.00 | P0 |
| PR-50 | An ACTIVE tenant's upgrade still prorates | Credit for unused time unchanged | P0 |

---

# 11. Entitlements — `onboard/tests/test_entitlements.py`

## 11.1 Counting

| ID | Scenario | Expected | P |
| :--- | :--- | :--- | :--- |
| EN-01 | Agents counted from live rows | Matches | P0 |
| EN-02 | **Deleting an agent frees the allowance** | Count drops — the delete-test | P0 |
| EN-03 | Users counted from `TenantUserMapping` | Matches | P0 |
| EN-04 | Contacts counted | Matches | P0 |
| EN-05 | Knowledge bases counted | Matches | P0 |
| EN-06 | Crawl pages counted from `KBWebsite` | Matches | P0 |
| EN-07 | Campaigns counted **this calendar month only** | Last month's excluded | P0 |
| EN-08 | Storage summed from file bytes, **rounded up** to GB | 10.2 GB reports 11 | P0 |
| EN-09 | Another tenant's rows never counted | Isolation | P0 |
| EN-10 | Unknown resource | Returns 0, logs | P1 |

## 11.2 Limits

| ID | Scenario | Expected | P |
| :--- | :--- | :--- | :--- |
| EN-11 | `limit_for` = included + purchased | Correct | P0 |
| EN-12 | `included = None` | Limit `None` (unlimited) | P0 |
| EN-13 | No `TenantEntitlement` row | Limit equals included | P0 |
| EN-14 | Included read from the **snapshot** | Raising the plan's cap does not raise an existing tenant's | P0 |

## 11.3 `apply_addon`

| ID | Scenario | Expected | P |
| :--- | :--- | :--- | :--- |
| EN-15 | First purchase | Row created with quantity, frozen price and step | P0 |
| EN-16 | Second purchase | Quantity accumulates; **price re-frozen to current** | P0 |
| EN-17 | `recurring_charge` computed per block | Rounded up | P0 |
| EN-18 | `purchased_at` and `subscription` recorded | Set | P1 |
| EN-19 | Resource with no catalogue row | Returns `None`, logs an error | P1 |
| EN-20 | **No ledger row is ever written** | Entitlements never touch the wallet | P0 |

## 11.4 `check`

| ID | Scenario | Expected | P |
| :--- | :--- | :--- | :--- |
| EN-21 | Under the limit | Returns, no exception | P0 |
| EN-22 | Exactly at the limit, requesting 1 more | Refused when enforced | P0 |
| EN-23 | Over the limit, **enforcement off** | Logs, does not raise | P0 |
| EN-24 | Over the limit, **enforcement on** | `QuotaExceeded` with used/limit/requested | P0 |
| EN-25 | `requested = N` counted, not 1 | Bulk request refused correctly | P0 |
| EN-26 | Unlimited resource | Never refuses, even for 999999 | P0 |
| EN-27 | No active subscription | Stays out of it — returns | P0 |
| EN-28 | Unknown resource | Logs and allows; **never raises** | P0 |
| EN-29 | Internal error in counting | Logs and allows | P0 |
| EN-30 | `is_enforced` reads the flag | True/False correctly | P1 |

## 11.5 `summary` / `recurring_total`

| ID | Scenario | Expected | P |
| :--- | :--- | :--- | :--- |
| EN-31 | `summary` returns one entry per resource | 7 entries | P1 |
| EN-32 | `with_usage=True` adds `used` and `remaining` | Correct arithmetic | P1 |
| EN-33 | `remaining` floors at 0 when over | Never negative | P1 |
| EN-34 | `remaining` is `None` for unlimited | Correct | P1 |
| EN-35 | `recurring_total` sums every add-on | Correct | P0 |
| EN-36 | `recurring_total` with no add-ons | 0.00 | P1 |

---

# 12. Purchases API — `onboard/tests/test_billing_api_purchases.py`

## 12.1 Top-up

| ID | Scenario | Expected | P |
| :--- | :--- | :--- | :--- |
| BP-01 | `top-up/quote/` valid | 200 with net/tax/total | P0 |
| BP-02 | Quote has **no side effects** | No transaction row | P0 |
| BP-03 | `credits` 0 or missing | 400 | P1 |
| BP-04 | Plan that does not sell credits | 409 `TOP_UPS_UNAVAILABLE` | P0 |
| BP-05 | No active subscription | 409 `NO_ACTIVE_SUBSCRIPTION` | P0 |
| BP-06 | Anonymous | 401/403 | P0 |
| BP-07 | `top-up/` creates a PENDING transaction | Type `top_up`, `credits_purchased` set | P0 |
| BP-08 | Response `amount` is **paise** | 708.00 → 70800 | P0 |
| BP-09 | `amount_display` is rupees | "708.00" | P1 |
| BP-10 | `expires_at` ≈ 30 minutes out | Set | P1 |
| BP-11 | **No credits granted before payment** | Balance unchanged | P0 |
| BP-12 | `amount_breakdown` stored on the transaction | Present | P0 |
| BP-13 | Second order while one is pending | 409 `PENDING_ORDER_EXISTS` with the order id | P0 |
| BP-14 | An expired pending order is cleared first | New order allowed | P0 |
| BP-15 | Gateway failure | 502 `GATEWAY_ERROR`, no orphan SUCCESSFUL row | P1 |

## 12.2 Add-ons

| ID | Scenario | Expected | P |
| :--- | :--- | :--- | :--- |
| BP-16 | `add-ons/quote/` valid | 200 with prorated and recurring figures | P0 |
| BP-17 | Both numbers present and different mid-cycle | `total` < `added_recurring_net` | P0 |
| BP-18 | Invalid resource string | 400 | P1 |
| BP-19 | Quantity 0 | 400 | P1 |
| BP-20 | Non-sellable resource | 409 `ADDON_UNAVAILABLE` | P0 |
| BP-21 | Beyond `max_additional` | 409 with the ceiling in the message | P0 |
| BP-22 | `add-ons/` creates a PENDING transaction | Type `addon`, breakdown stored | P0 |
| BP-23 | **No capacity granted before payment** | No `TenantEntitlement` row | P0 |
| BP-24 | Prorated total below ₹1 | **Granted free**, `charged: false`, no order | P0 |
| BP-25 | Second order while one is pending | 409 | P0 |
| BP-26 | Free plan cannot buy capacity | 409 | P0 |
| BP-27 | Tenant isolation on every purchase endpoint | Own subscription only | P0 |
| BP-28 | Quote and order agree on price | Same total | P0 |
| BP-29 | Breakdown records `additional_units` and `resource` | Needed by the webhook | P0 |
| BP-30 | Order receipt is truncated to gateway limits | ≤ 40 chars | P2 |

---

# 13. Subscription API — `onboard/tests/test_billing_api_subscription.py`

| ID | Scenario | Expected | P |
| :--- | :--- | :--- | :--- |
| BS-01 | `GET /subscription/` | 200 with plan, cycle, credits, entitlements | P0 |
| BS-02 | `days_remaining` and `renewable_now` | Correct | P1 |
| BS-03 | Scheduled change reported | Plan code and effective date | P1 |
| BS-04 | `entitlements` include live usage | `used` / `remaining` present | P1 |
| BS-05 | `addons_recurring_net` | Matches the sum | P1 |
| BS-06 | No subscription at all | 409 `NO_SUBSCRIPTION` | P0 |
| BS-07 | Anonymous | 401/403 | P0 |
| BS-08 | Tenant isolation | Own subscription only | P0 |
| BS-09 | `eligible-plans/` lists every active plan | 5 entries | P1 |
| BS-10 | Current plan tagged `current` | Correct | P1 |
| BS-11 | Upgrade tagged `paid_change` with an amount | Correct | P0 |
| BS-12 | Downgrade tagged `scheduled_change`, 0.00 | Correct | P0 |
| BS-13 | Unpriceable plan tagged `unavailable` with a reason | Still listed | P2 |
| BS-14 | Inactive plans excluded | Not listed | P1 |
| BS-15 | `preview-change/` returns money **and** credits | Both blocks | P0 |
| BS-16 | Preview writes nothing | No transaction, no schedule | P0 |
| BS-17 | Preview warnings on credit loss | Text present | P0 |
| BS-18 | Preview of the current plan | 409 `PLAN_UNCHANGED` | P1 |
| BS-19 | Preview of an unknown plan id | 404 `PLAN_NOT_FOUND` | P1 |
| BS-20 | `change-plan/` upgrade | Order created; **plan unchanged until payment** | P0 |
| BS-21 | Server recomputes the quote | A client-supplied amount is ignored | P0 |
| BS-22 | `change-plan/` downgrade | Scheduled; `charged: false`; no order | P0 |
| BS-23 | `change-plan/` sub-₹1 upgrade | Applied immediately, no order | P1 |
| BS-24 | Change while an order is pending | 409 `PENDING_ORDER_EXISTS` | P0 |
| BS-25 | `DELETE /scheduled-change/` | Cleared | P0 |
| BS-26 | Delete with nothing scheduled | 409 `NO_SCHEDULED_CHANGE` | P1 |
| BS-27 | `renew/` inside the window | Order created | P0 |
| BS-28 | `renew/` outside the window | 409 `RENEWAL_WINDOW_NOT_OPEN` with days left | P0 |
| BS-29 | Renewal amount includes add-ons | plan + recurring | P0 |
| BS-30 | Expired subscription can renew | Allowed | P0 |
| BS-31 | Renew while an order is pending | 409 | P0 |
| BS-32 | Cancelled subscriptions excluded from the renew fallback | Not selected | P2 |

## 13.1 Expired tenants — what they can see and do

The rule: **see everything, buy only a plan.** A lapsed tenant needs to understand why
they are blocked and what it costs to return; renting capacity for a cycle that is not
running does not.

| ID | Scenario | Expected | P |
| :--- | :--- | :--- | :--- |
| BS-33 | `GET /subscription/` while expired | **200**, not 409 | P0 |
| BS-34 | It reports the lapsed state | `status: expired`, and which plan they had | P0 |
| BS-35 | Credits still visible | Purchased balance shown | P0 |
| BS-36 | `renewable_now` past `end_date` | `true` | P1 |
| BS-37 | `days_remaining` past `end_date` | Floors at 0, never negative | P1 |
| BS-38 | `GET /eligible-plans/` while expired | 200, every active plan listed | P0 |
| BS-39 | Every plan priced at full price | No credit for unused time | P0 |
| BS-40 | `preview-change/` while expired | 200 with `reactivation: true` | P0 |
| BS-41 | `change-plan/` to a **different** plan while expired | Order created at full price | P0 |
| BS-42 | Plan unchanged until that payment clears | Still expired beforehand | P0 |
| BS-43 | Payment reactivates them on the new plan | ACTIVE, old row CANCELLED and linked | P0 |
| BS-44 | `change-plan/` to the **same** plan while expired | 409 `PLAN_UNCHANGED`, message points at `renew/` | P0 |
| BS-45 | `renew/` while expired | 200; **extends the existing row**, no second subscription | P0 |
| BS-46 | `top-up/` while expired | **409 `NO_ACTIVE_SUBSCRIPTION`** | P0 |
| BS-47 | `add-ons/` while expired | 409 | P0 |
| BS-48 | `GET /billing/wallet/` while expired | 200 — the wallet needs no subscription | P0 |
| BS-49 | Tenant with **no subscription ever** | 409 `NO_SUBSCRIPTION` — a distinct code from the expired case | P1 |
| BS-50 | Tenant whose only rows are CANCELLED | Treated as having none | P1 |
| BS-51 | An ACTIVE tenant is unaffected by any of this | Renewal window and scheduled downgrades behave as before | P0 |
| BS-52 | A second plan change right after reactivating | Treated as an ordinary active-tenant change (prorated, or scheduled if cheaper) — not a second reactivation | P0 |

---

# 14. Webhook — `onboard/tests/test_billing_webhook.py`

## 14.1 Signature and idempotency

| ID | Scenario | Expected | P |
| :--- | :--- | :--- | :--- |
| BW-01 | Valid signature (or unsigned allowed) | 200 | P0 |
| BW-02 | Invalid signature, verification on | 400 `SIGNATURE_CHECK_FAILED` | P0 |
| BW-03 | Signature computed over the **raw body** | Reading `request.data` first must not break it | P0 |
| BW-04 | Same `event_id` twice | Second returns 200 "already processed", **nothing re-applied** | P0 |
| BW-05 | Only one webhook log row per `event_id` | Confirmed | P0 |
| BW-06 | Fresh `event_id`, already-settled order | 200; **credits not granted twice** | P0 |
| BW-07 | Event with no `id` | Processed (many NULLs allowed) | P1 |

## 14.2 Dispatch

| ID | Scenario | Expected | P |
| :--- | :--- | :--- | :--- |
| BW-08 | `onboarding` capture | User, tenant, subscription, credits | P0 |
| BW-09 | Onboarding where the TempUser is already consumed | Returns early, no duplicate account | P0 |
| BW-10 | `top_up` capture | Credits into the **purchased** bucket | P0 |
| BW-11 | **Top-up does not run the signup path** | No tenant/user created | P0 |
| BW-12 | Top-up with `credits_purchased = 0` | Logged as an error, nothing granted | P1 |
| BW-13 | `addon` capture | `TenantEntitlement` created from the stored breakdown | P0 |
| BW-14 | Add-on with an unusable breakdown | Logged, capacity not granted | P1 |
| BW-15 | `renewal` capture | Cycle extended, credits granted, wallet unfrozen | P0 |
| BW-16 | Renewal with no subscription attached | Logged, nothing changed | P1 |
| BW-17 | `upgrade` capture | Plan changed, old closed | P0 |
| BW-18 | **Upgrade clears a pending downgrade** | `scheduled_plan` null afterwards | P0 |
| BW-19 | Upgrade missing `target_plan` | Logged, no change | P1 |
| BW-20 | Unknown transaction type | `_apply_unimplemented` logs; payment still recorded | P1 |

## 14.3 Robustness

| ID | Scenario | Expected | P |
| :--- | :--- | :--- | :--- |
| BW-21 | Unknown order id | 200, no crash | P0 |
| BW-22 | Unhandled event type | 200 | P1 |
| BW-23 | Malformed payload | **200, never 500**; error on the log row | P0 |
| BW-24 | Processing raises mid-way | Transaction rolled back; 200 returned | P0 |
| BW-25 | `payment.failed` | Order marked FAILED | P0 |
| BW-26 | `payment.failed` after a capture | **Ignored** — cannot un-succeed a paid order | P0 |
| BW-27 | Capture on an already-SUCCESSFUL order | No re-application | P0 |
| BW-28 | Transaction is locked during processing | `select_for_update` used | P1 |

---

# 15. Billing tasks — `onboard/tests/test_billing_tasks.py`

| ID | Scenario | Expected | P |
| :--- | :--- | :--- | :--- |
| BT-01 | `process_billing_cycle` applies a due scheduled change | Plan changed, still ACTIVE | P0 |
| BT-02 | **A tenant with a due scheduled change is not expired** | The ordering rule | P0 |
| BT-03 | Lapsed subscription with no schedule | Expired | P0 |
| BT-04 | Not yet due | Untouched | P0 |
| BT-05 | `renews_without_payment` plan lapses | **Auto-renewed**, not expired | P0 |
| BT-06 | One failing subscription | Others still processed; counted as failed | P0 |
| BT-07 | Each subscription in its own transaction | A failure does not roll back the others | P0 |
| BT-08 | Re-run after processing | Idempotent, no double effect | P0 |
| BT-09 | Return value counts | changed / renewed / expired / failed | P1 |
| BT-10 | Row re-checked under the lock | A concurrently-changed row is skipped | P1 |
| BT-11 | `expire_stale_billing_orders` cancels expired PENDING | Status CANCELED | P0 |
| BT-12 | Unexpired PENDING untouched | Still pending | P0 |
| BT-13 | SUCCESSFUL orders untouched | Unchanged | P0 |
| BT-14 | After the sweep a new order is allowed | 200 | P0 |
| BT-15 | `send_renewal_reminders` at T-7 / T-3 / T-1 | One notice each | P1 |
| BT-16 | T-5 (not a reminder day) | Nothing sent | P1 |
| BT-17 | Second run the same day | **Deduplicated** | P1 |
| BT-18 | Expired subscriptions not reminded | Skipped | P1 |
| BT-19 | Notification failure for one tenant | Others still notified | P1 |
| BT-20 | Beat schedule contains all six billing entries | Names match the task names | P1 |
| BT-21 | Task names stable (`onboard.tasks.*`) | Match the schedule | P1 |
| BT-22 | Cycle task on a tenant with no wallet | Does not crash | P2 |

---

# 16. Payment settings and checks — `onboard/tests/test_payment_settings.py`

| ID | Scenario | Expected | P |
| :--- | :--- | :--- | :--- |
| PS-01 | `RAZORPAY_KEY_ID` unset | **Django refuses to boot** | P0 |
| PS-02 | `RAZORPAY_KEY_SECRET` unset | Refuses to boot | P0 |
| PS-03 | No dummy default remains in settings | Absent | P0 |
| PS-04 | No `getattr(..., "dummy…")` fallback in the views | Absent | P0 |
| PS-05 | `PAYMENTS_ALLOW_UNSIGNED` defaults to `False` | Fails closed | P0 |
| PS-06 | Production settings with the flag on | `ImproperlyConfigured` at import | P0 |
| PS-07 | Production settings with an empty webhook secret | `ImproperlyConfigured` | P0 |
| PS-08 | `onboard.E001` — unsigned while `DEBUG=False` | Error raised | P0 |
| PS-09 | `onboard.E001` silent in DEBUG | No error | P1 |
| PS-10 | `onboard.E002` — missing secret while verifying | Error raised | P0 |
| PS-11 | `onboard.W002` — same in DEBUG | Warning, not error | P1 |
| PS-12 | Both checks silent when correctly configured | Clean | P1 |
| PS-13 | **Checks skipped during a test run** | The suite is not blocked by its own settings | P0 |
| PS-14 | Signature verification keyed on the flag, not on a credential value | Confirmed | P0 |

---

# 17. Management commands — `onboard/tests/test_billing_commands.py`

| ID | Scenario | Expected | P |
| :--- | :--- | :--- | :--- |
| CM-01 | `reconcile_wallets` — clean | Exit 0, success message | P0 |
| CM-02 | Injected drift | Non-zero exit, drift reported | P0 |
| CM-03 | `--fix` posts an `ADMIN_ADJUSTMENT` | Balance corrected, correction auditable | P0 |
| CM-04 | `--fix` without `--reason` | Still works; reason recorded as empty | P2 |
| CM-05 | Query count is O(1) in wallet count | Two queries, not 3n | P1 |
| CM-06 | `audit_billing_readiness` — clean tenants | Reported ready | P0 |
| CM-07 | Tenant with ledger drift | Reported blocked | P0 |
| CM-08 | Tenant over a limit | Reported blocked with the numbers | P0 |
| CM-09 | Tenant with no stamped rate card | Reported blocked | P0 |
| CM-10 | Tenant with no subscription | Counted separately | P1 |
| CM-11 | Catalogue section flags a plan with no rates | Reported | P0 |
| CM-12 | `--enable-safe` enables both flags for clean tenants only | Blocked tenants untouched | P0 |
| CM-13 | `--verbose` lists per-resource usage | Present | P2 |
| CM-14 | `backfill_wallet_stamps` stamps unstamped wallets | Rate card present afterwards | P0 |
| CM-15 | Already-stamped wallets skipped | Not re-stamped | P0 |
| CM-16 | Soft-deleted tenants excluded | Not processed | P1 |
| CM-17 | `--grant-allocations` grants once | Idempotent on re-run | P0 |
| CM-18 | `--dry-run` rolls everything back | No changes persist | P0 |

---

# 18. Notifications — `onboard/tests/test_billing_notifications.py`

| ID | Scenario | Expected | P |
| :--- | :--- | :--- | :--- |
| BN-01 | `notify_renewal_due` creates one notice per tenant user | Count matches | P1 |
| BN-02 | Type `BILLING` | Correct | P1 |
| BN-03 | Deduplicated within a day | Second call returns 0 | P1 |
| BN-04 | `notify_expired` | Notice created, mentions kept credits | P1 |
| BN-05 | `notify_low_balance` | Type `CREDITS` | P1 |
| BN-06 | `notify_credits_added` | Type `CREDITS`, states the amount | P1 |
| BN-07 | `notify_plan_changed` | Names the new plan | P1 |
| BN-08 | Tenant with no users | Returns 0, logs, no crash | P1 |
| BN-09 | **In-app row written before push is queued** | Notice survives a push failure | P0 |
| BN-10 | Push queued with a `route` | Data payload carries it | P1 |
| BN-11 | Push failure | Logged; in-app notice still created | P0 |
| BN-12 | Notification failure never breaks a lifecycle change | `_announce` swallows it | P0 |
| BN-13 | Top-up notice fires only on a **real** grant | Not on a replayed webhook | P0 |
| BN-14 | Expiry notice fired from `expire()` | Present | P1 |
| BN-15 | Plan-change notice fired from `apply_plan_change()` | Present | P1 |

---

# 19. Credit consumption — `core/tests/billing/test_credit_consumption.py`

| ID | Scenario | Expected | P |
| :--- | :--- | :--- | :--- |
| CC-01 | Call log created with a duration | Wallet charged once | P0 |
| CC-02 | Charge amount matches the tier and duration | Correct arithmetic | P0 |
| CC-03 | **Call log's tier beats the agent's** | Billed at what actually ran | P0 |
| CC-04 | No tier on the log | Agent's tier used | P0 |
| CC-05 | Same `provider_call_id` posted twice | **Charged once** | P0 |
| CC-06 | Zero-duration call | Not charged, no error | P0 |
| CC-07 | Unpriceable tier | Logged and swallowed; **request still succeeds** | P0 |
| CC-08 | Wallet service raises | Request still succeeds | P0 |
| CC-09 | Insufficient balance, enforcement off | Call log created, nothing charged, logged | P0 |
| CC-10 | Ledger row carries the rating snapshot | Present | P0 |
| CC-11 | Ledger row carries agent id and direction in metadata | Present | P2 |
| CC-12 | Charge is scoped to the call log's tenant | Never another tenant's wallet | P0 |
| CC-13 | VB_AGENT-created call log | Charged to the header tenant | P0 |
| CC-14 | `assert_can_consume` at campaign start, sufficient credit | Campaign starts | P0 |
| CC-15 | Zero balance, enforcement off | **Campaign still starts**, logged | P0 |
| CC-16 | Zero balance, enforcement on | Campaign not started, stays SCHEDULED | P0 |
| CC-17 | Frozen wallet, enforcement on | Campaign not started | P0 |
| CC-18 | Unpriced tier at campaign start | Starts anyway; misconfiguration must not silently skip campaigns | P1 |
| CC-19 | Credit check error at campaign start | Starts anyway | P0 |
| CC-20 | Runner context includes a `credits` block | balance, is_frozen, service_tier, credits_per_minute | P1 |
| CC-21 | Credit context failure | `credits: null`, handoff unaffected | P0 |
| CC-22 | Invariant holds after many consumption cycles | `SUM(ledger) == balance` | P0 |

---

# 20. Entitlement guards — `core/tests/billing/test_entitlement_guards.py`

| ID | Scenario | Expected | P |
| :--- | :--- | :--- | :--- |
| EG-01 | Create an agent under the limit | 201 | P0 |
| EG-02 | Create an agent over the limit, enforcement on | **402 `QUOTA_EXCEEDED`** | P0 |
| EG-03 | Over the limit, enforcement off | 201, logged | P0 |
| EG-04 | Refusal body names resource, used, limit | All present | P1 |
| EG-05 | Nothing is created on refusal | Count unchanged | P0 |
| EG-06 | Contacts guarded | Same behaviour | P0 |
| EG-07 | Knowledge bases guarded | Same | P0 |
| EG-08 | Campaigns guarded | Same | P0 |
| EG-09 | Users guarded | 402 on the 4th of 3 seats | P0 |
| EG-10 | **KB with N websites consumes N crawl pages** | Both checked | P0 |
| EG-11 | KB exceeding the crawl-page limit | 402, nothing created | P0 |
| EG-12 | KB with no websites | Only the KB is checked | P1 |
| EG-13 | Buying capacity raises the limit | Create then succeeds | P0 |
| EG-14 | Unlimited resource | Never refused | P0 |
| EG-15 | No subscription | Not refused by the guard | P0 |
| EG-16 | Superuser with no tenant mapping | Not refused | P1 |
| EG-17 | Tenant A's usage does not affect tenant B | Isolation | P0 |
| EG-18 | Updates are not blocked, only creates | PATCH under/over limit both allowed | P1 |
| EG-19 | Deletes are never blocked | Always allowed | P1 |
| EG-20 | Deleting then re-creating at the limit | Allowed after the delete | P0 |
| EG-21 | `CustomUserViewSet` guard fires despite its own `perform_create` | **The shadowing case** | P0 |
| EG-22 | Guard error never blocks a create | Logged and allowed | P0 |
| EG-23 | 402 rather than 403 | Status is exactly 402 | P1 |
| EG-24 | Campaign counting is windowed | Last month's campaigns do not count | P1 |

---

# 21. End-to-end journeys — `onboard/tests/test_billing_journeys.py`

Full paths through the system, each asserting the invariant at every step.

| ID | Journey | P |
| :--- | :--- | :--- |
| E2E-01 | **Free signup → call → upgrade to paid → call at the new rate.** Trial credits granted, 20 charged, upgrade prorated and paid, credits rolled over, next call priced at the new plan | P0 |
| E2E-02 | **Paid signup → top-up → spend across both buckets → renew.** Allocated drains first, purchased survives the renewal, forfeit correct | P0 |
| E2E-03 | **Signup → buy storage → renew → verify the renewal includes the add-on.** Recurring amount correct | P0 |
| E2E-04 | **Lapse → expire → renew.** Plan credits forfeited, purchased kept, wallet frozen then unfrozen, access restored | P0 |
| E2E-05 | **Schedule a downgrade → cycle ends → downgrade applies.** Not expired, rate card re-stamped, rollover per the old plan | P0 |
| E2E-06 | **Schedule a downgrade → upgrade instead.** Scheduled change cleared; the downgrade never fires | P0 |
| E2E-07 | **Webhook race:** `verify_payment` and the webhook both complete onboarding | One account, one subscription, credits once | P0 |
| E2E-08 | **Every webhook redelivered twice** at each step of a full lifecycle | No double credits, no double capacity, no double plan change | P0 |
| E2E-09 | **Enforcement flip:** run a full cycle with both flags off, then on | Nothing refused before, correctly refused after | P0 |
| E2E-10 | **Hit every entitlement limit, buy capacity, continue.** Refused, purchased, allowed | P0 |
| E2E-11 | **Pay-as-you-go shape:** 0 allocation, top-ups only, `allow_use_after_expiry`, `renews_without_payment` | Cycle rolls at ₹0, credits stay spendable | P1 |
| E2E-12 | **Audit → backfill → audit → enable.** Blocked, repaired, ready, enforced | P0 |
| E2E-13 | **Lapse → see state → return on a bigger plan.** Expires holding purchased credits, reads its own expired state, picks a different plan, pays full price once, is reactivated, spends at the new rate. **The credits bought before the lapse survive the entire journey** | P0 |
| E2E-14 | **Lapse → renew the same plan.** One subscription row throughout, cycle extended, wallet unfrozen, fresh allocation granted | P0 |
| E2E-15 | **Lapse → return on the free plan.** Activates immediately with no order and no payment | P1 |
| E2E-16 | **Lapse twice.** Two lapse-and-return cycles leave one subscription row when renewing, and a linked chain when switching plans — never an unexplained pile of rows | P1 |

---

# 22. Fixtures and shared setup

Proposed `onboard/tests/billing_factories.py`, so 500 cases do not each rebuild the world:

| Helper | Provides |
| :--- | :--- |
| `make_tenant(name)` | Tenant + user + authenticated `APIClient` |
| `make_plan(**overrides)` | Plan with entitlements and a full rate card |
| `subscribe(tenant, plan)` | Runs `activate()` and returns the subscription |
| `pay(order_id)` | Delivers a `payment.captured` webhook |
| `age_subscription(sub, days_in)` | Moves the cycle window so proration has something to prorate |
| `assert_invariant(tenant)` | `SUM(ledger) == balance`, per bucket |

**`assert_invariant` should run at the end of every test that moves credits** — that is
the single assertion most likely to catch a regression nobody predicted.

---

# 23. Gaps — resolved

Every item raised in review, and what was decided.

| # | Gap | Decision |
| :--- | :--- | :--- |
| 1 | Real Razorpay API calls | **Covered** — §25, opt-in against test-mode credentials |
| 2 | FCM push delivery | **Covered** — §26, opt-in against the real Firebase project |
| 3 | Admin UI actions | **Covered** — §27 |
| 4 | Load and query counts | **Covered** — §28 |
| 5 | Yearly proration at scale | **Covered** — §29 |
| 6 | Concurrency beyond WS-52 | **Covered** — §30, without threads |
| 7 | Migration replay from zero | **Covered** — §31 |
| 8 | Currency other than INR | **Out of scope** — §32 documents INR as a deliberate assumption |

# 24. Suggested build order

1. **Fixtures** (§22) — everything else depends on them
2. **P0 model and constraint cases** (§1, §2, §8) — fastest, catch schema regressions
3. **P0 service cases** (§3, §5, §9, §10, §11) — the logic that moves money
4. **P0 API and webhook cases** (§6, §12, §13, §14)
5. **P0 consumption and guards** (§19, §20)
6. **End-to-end journeys** (§21)
7. **P1 and P2 throughout**
8. **Tasks, commands, settings** (§7, §15, §16, §17, §18)

Stopping after step 5 would still cover every path where money moves.

---

**Waiting on your review.** Tell me what to add, cut or reprioritise, and I will start
implementing — I would suggest fixtures plus §1–§2 first so you can see the shape
before I write the other 480.

---

# 25. Live Razorpay sandbox — `onboard/tests/test_razorpay_live.py`

**Opt-in.** The only tests here that leave the machine. They run against Razorpay's
**test mode** using the `rzp_test_…` credentials already in `.env`, and are skipped
unless `RUN_LIVE_GATEWAY_TESTS=1` is set — so CI, a plane, and a colleague without
credentials all still get a green suite.

Everything else in this document stubs the gateway. These exist to answer the one
question stubs cannot: *does our request actually satisfy Razorpay's API?*

| ID | Scenario | Expected | P |
| :--- | :--- | :--- | :--- |
| RZ-01 | `create_order` for Rs 708 | Real `order_…` id returned | P0 |
| RZ-02 | Amount sent in **paise** | Razorpay echoes `amount: 70800` | P0 |
| RZ-03 | Currency and `payment_capture` accepted | Order created as INR, auto-capture | P0 |
| RZ-04 | `receipt` longer than the field allows | Truncation keeps the call valid | P1 |
| RZ-05 | `notes` round-trip | Tenant id and type readable on the fetched order | P1 |
| RZ-06 | Order below Rs 1 | Refused **by us** before the call — proves `MINIMUM_CHARGE` matches reality | P0 |
| RZ-07 | Fetch the created order back | Status `created`, amount matches | P1 |
| RZ-08 | Bad credentials | Raises; our caller surfaces `502 GATEWAY_ERROR` | P1 |
| RZ-09 | **Webhook signature verified against a real HMAC** | A payload signed with the real secret passes verification | P0 |
| RZ-10 | Tampered body, same signature | Rejected | P0 |
| RZ-11 | Payment-signature verification over order id + payment id | Real HMAC accepted, wrong one rejected | P0 |
| RZ-12 | Network failure mid-call | Raises cleanly; no orphan SUCCESSFUL transaction | P0 |

> **RZ-09 to RZ-11 matter most.** Every other webhook test in this document runs with
> `PAYMENTS_ALLOW_UNSIGNED=True`, so the signature path is never exercised. These are
> the only cases proving verification works at all — including that we hash the **raw
> body** rather than a re-serialised dict, which is a bug I have already made once.

**Still not covered:** completing a payment. That needs Razorpay Checkout in a browser
and is a manual QA step. RZ-01 to RZ-08 create real test-mode orders that are never
paid.

---

# 26. Live FCM push — `core/tests/billing/test_push_live.py`

**Opt-in**, same pattern: skipped unless `RUN_LIVE_PUSH_TESTS=1`. Uses the real
`FIREBASE_CREDENTIALS` already configured for project `voicebot-dev-2f704`.

| ID | Scenario | Expected | P |
| :--- | :--- | :--- | :--- |
| PU-01 | Firebase app initialises from the configured credentials | No exception; project id matches | P0 |
| PU-02 | `probe_token` with a syntactically invalid token | Classified invalid, not a crash | P0 |
| PU-03 | `probe_token` with a well-formed but unregistered token | Classified `unregistered` | P0 |
| PU-04 | Billing push to a tenant with registered tokens | FCM accepts or returns per-token errors; nothing escapes | P0 |
| PU-05 | Unregistered tokens marked inactive by `_apply_results` | Rows updated | P1 |
| PU-06 | Data payload carries `type` and `route` | Present in the message | P1 |
| PU-07 | `NotificationLog` marked SENT on acceptance | Recorded | P1 |
| PU-08 | Marked FAILED once retries are exhausted | Recorded | P1 |
| PU-09 | Tenant with no device tokens | Returns 0, no call attempted | P1 |

> **What "sent" means.** FCM exposes no per-device delivery receipt in this API, so a
> `SENT` row records that **Firebase accepted the message** — not that a phone showed
> it. No automated test can assert the latter.
>
> The 4 device tokens currently in your database are almost certainly stale. That is
> *useful*: PU-03 and PU-05 exercise the unregistered-token branch, which is the one
> most likely to be wrong and the one that runs most often in production.

---

# 27. Admin actions — `wallet/tests/test_admin_actions.py`, `onboard/tests/test_admin_actions.py`

The admin glue is untested even though the services beneath it are — and these actions
are how support moves real money.

## 27.1 Wallet admin

| ID | Scenario | Expected | P |
| :--- | :--- | :--- | :--- |
| AD-01 | Grant with amount and reason | Credits added **through the service**; ledger row written | P0 |
| AD-02 | `performed_by` recorded | The acting staff user | P0 |
| AD-03 | Grant with no amount | Error message, nothing moved | P0 |
| AD-04 | Grant with no reason | Refused — an unexplained balance change is worse than none | P0 |
| AD-05 | Non-numeric amount | Error message, nothing moved | P1 |
| AD-06 | Amount below 1 | Refused | P1 |
| AD-07 | Deduct | Credits removed, allocated first | P0 |
| AD-08 | Deduct more than held | Clamped; balance never negative | P0 |
| AD-09 | Action across several wallets | Each processed; summary counts correct | P1 |
| AD-10 | **Double-submitted form within one second** | Idempotency reference absorbs it; charged once | P0 |
| AD-11 | Deliberate repeat a minute later | Applies again | P1 |
| AD-12 | Freeze | `is_frozen` set, reason names the staff user | P1 |
| AD-13 | Unfreeze | Reason and timestamp cleared | P1 |
| AD-14 | Balance fields read-only in the form | Not editable | P0 |
| AD-15 | Service failure during an action | Surfaced to the operator; other wallets unaffected | P1 |
| AD-16 | Ledger inline is read-only | No add permission | P1 |

## 27.2 Credit rate admin

| ID | Scenario | Expected | P |
| :--- | :--- | :--- | :--- |
| AD-17 | Supersede closes the selected rate | `effective_to` set | P0 |
| AD-18 | **Supersede a rate created moments ago** | Clamped past `effective_from`; no IntegrityError | P0 |
| AD-19 | Supersede an already-closed rate | Skipped, counted separately | P1 |
| AD-20 | New open rate after superseding | Uniqueness slot freed | P0 |
| AD-21 | Message warns the plan now has no price | Text present | P1 |
| AD-22 | Rate inline on the plan page | Editable there | P2 |

## 27.3 Plan and entitlement admin

| ID | Scenario | Expected | P |
| :--- | :--- | :--- | :--- |
| AD-23 | Entitlement inline on the plan page | Add and edit work | P1 |
| AD-24 | Add-on enabled on an unlimited allowance | Refused by the constraint | P0 |
| AD-25 | `TenantEntitlement` price fields read-only | Not editable | P0 |
| AD-26 | Ledger admin: add, change, delete all forbidden | Three permissions `False` | P0 |

---

# 28. Query budgets — `wallet/tests/test_query_budget.py`

The call path runs on every call. A regression here is invisible until volume makes it
expensive.

| ID | Scenario | Budget | P |
| :--- | :--- | :--- | :--- |
| QB-01 | `resolve_rate` from a stamped wallet | **0 queries** — reads the in-memory snapshot | P0 |
| QB-02 | `rate_quantity` | 0 queries | P0 |
| QB-03 | `quote()` end to end | 1 query (the wallet) | P1 |
| QB-04 | `consume()` | Exact count asserted: lock, probe, wallet update, ledger insert | P0 |
| QB-05 | `consume()` never queries `CreditRate` | The catalogue stays off the hot path | P0 |
| QB-06 | `assert_can_consume` | Wallet + settings, no catalogue | P0 |
| QB-07 | `_enforced()` creates no `TenantSettings` row | Read-only on the hot path | P0 |
| QB-08 | `GET /billing/wallet/` | Constant, independent of ledger size | P1 |
| QB-09 | `GET /billing/wallet/ledger/` | Constant per page — no N+1 on tenant or wallet | P1 |
| QB-10 | `GET /billing/subscription/` with usage | One count per resource, not per row | P1 |
| QB-11 | `eligible-plans/` with 5 plans | No per-plan rate query | P1 |
| QB-12 | `reconcile_wallets` over 100 wallets | **2 queries total**, not 3n | P0 |
| QB-13 | `summary(with_usage=True)` | One query per resource | P1 |
| QB-14 | Entitlement guard on create | Bounded addition to the request | P1 |

Written with `assertNumQueries` asserting an **exact** number, not a ceiling — a budget
that only fails when it doubles is a budget nobody notices breaking.

---

# 29. Yearly proration, day by day — `onboard/tests/test_pricing_yearly.py`

PR-33/34 prove only the *direction*. A yearly cycle is 365 days of arithmetic where a
rounding error is small enough to hide and large enough to matter.

| ID | Scenario | Expected | P |
| :--- | :--- | :--- | :--- |
| YP-01 | Yearly plan, upgrade on day 1 | Credit approximately the full net price | P0 |
| YP-02 | Day 182 (half) | Credit approximately half, to the paisa | P0 |
| YP-03 | Day 364 | Credit approximately 1/365th | P0 |
| YP-04 | Day 365, last moment | Credit approximately 0; **never negative** | P0 |
| YP-05 | Past `end_date` | Ratio 0; charged the full new price | P0 |
| YP-06 | **Every 30th day across a full year** | Credit decreases monotonically, never negative, never above the net price | P0 |
| YP-07 | Sum of daily deltas approximates the net price | No systematic drift from 4-dp ratio quantisation | P0 |
| YP-08 | Monthly to yearly | Credit from the monthly plan only; charged the full yearly | P0 |
| YP-09 | Yearly to monthly | `scheduled_change`, zero, never a refund | P0 |
| YP-10 | Yearly to yearly (Starter to Professional) | Prorated on the yearly prices | P0 |
| YP-11 | Leap year — cycle spanning 29 February | 366-day cycle handled; ratio still at most 1 | P1 |
| YP-12 | Cycle crossing a DST boundary | Ratio computed on UTC instants, unaffected | P1 |
| YP-13 | Add-ons on a yearly plan | Prorated over the yearly cycle, not a month | P0 |
| YP-14 | Yearly renewal allocation | 12x the monthly allocation | P0 |
| YP-15 | Yearly rollover cap | Applied against the yearly cap | P1 |
| YP-16 | Rounding never favours either side by more than Rs 0.01 | Bounded error | P1 |

---

# 30. Concurrency — `wallet/tests/test_concurrency.py`

**The trap.** Django's `TestCase` wraps every test in a transaction that is rolled back
at the end, so two threads cannot see each other's uncommitted data — the race you want
to test *cannot happen*. `TransactionTestCase` restores visibility but truncates tables
between tests, which is slow, and naive thread-timing assertions are the classic source
of CI flakes.

**The fix: make the interleaving deterministic, and prove the lock separately.** Not
"run two threads and hope one loses" — *choose* the interleaving with synchronisation
primitives, so the same sequence happens on every run, on every machine.

> **Correction to an earlier draft of this section.** It proposed patching a seam inside
> `consume()` and calling it recursively from the same connection. That proves nothing:
> a second `SELECT … FOR UPDATE` on the same row **inside the same transaction** does
> not block — it is the same transaction, so it succeeds immediately. Any interleaving
> test must use **two real database connections**, or it is testing nothing at all.

## Technique A — deterministic hand-off between two connections

Threads are used, but they never race: `threading.Event` decides the order.

```python
class ConsumeInterleavingTests(TransactionTestCase):
    def test_only_one_of_two_concurrent_consumes_succeeds(self):
        # Wallet holds exactly one call's worth of credit.
        a_has_lock = threading.Event()
        a_may_finish = threading.Event()
        outcomes = {}

        def worker_a():
            with transaction.atomic():
                wallet_service.consume(...)      # takes the row lock
                a_has_lock.set()                 # tell B to start
                a_may_finish.wait(timeout=5)     # hold the lock open
            outcomes["a"] = "committed"
            connection.close()

        def worker_b():
            a_has_lock.wait(timeout=5)           # only start once A holds the lock
            # This call blocks on the lock until A commits, then re-reads a balance
            # that A has already spent.
            outcomes["b"] = wallet_service.consume(...)
            connection.close()

        # start both, release A, join, then assert
```

**What makes it deterministic:** B never begins until A holds the lock, and A never
commits until B is definitely waiting on it. There is no sleep and no assumption about
scheduler timing — only ordering that the events enforce. The `timeout=5` on each wait
turns a genuine deadlock into a clear test failure rather than a hung suite.

**Each thread must `connection.close()`.** Django opens a connection per thread and
`TransactionTestCase` will not tidy them up for you; leaked connections are what make
the *next* test fail mysteriously.

## Technique B — prove the row is locked, with `nowait`

Cheaper than A and proves the lock directly rather than by its consequences.

```python
with transaction.atomic():
    Wallet.objects.select_for_update().get(pk=wallet.pk)   # connection 1 holds it
    with self.assertRaises(OperationalError):              # connection 2
        run_on_other_connection(
            lambda: Wallet.objects.select_for_update(nowait=True).get(pk=wallet.pk)
        )
```

`nowait=True` turns "wait for the lock" into "raise immediately", which is what removes
the timing element entirely.

## Technique C — assert the lock clause is present

The cheapest of the three, runs under plain `TestCase`, and catches the one regression
most likely to happen: somebody removing `select_for_update()` while refactoring.

```python
with CaptureQueriesContext(connection) as queries:
    wallet_service.consume(...)
self.assertTrue(any("FOR UPDATE" in q["sql"] for q in queries))
```

## Cases

| ID | Scenario | Technique | Expected | P |
| :--- | :--- | :--- | :--- | :--- |
| CN-01 | A holds the wallet lock; B tries `nowait` | B | B raises — the row is genuinely locked | P0 |
| CN-02 | `consume` issues `SELECT … FOR UPDATE` | C | Present in the query | P0 |
| CN-03 | `grant` issues it | C | Present | P0 |
| CN-04 | Two consumes, one credit left | A | **Exactly one succeeds; balance never negative** | P0 |
| CN-05 | Two consumes, both affordable | A | Both succeed; balance correct | P0 |
| CN-06 | Interleaved grant and consume | A | Both applied; invariant holds | P0 |
| CN-07 | Two concurrent `activate()` for one tenant | A | One wins; the other returns the existing subscription, no IntegrityError | P0 |
| CN-08 | Two workers in `process_billing_cycle` on one subscription | A | The re-check under the lock means only one acts | P0 |
| CN-09 | Same webhook event delivered on two connections at once | A | One processes; the other short-circuits on `event_id` | P0 |
| CN-10 | Invariant after every interleaving above | — | `SUM(ledger) == balance`, per bucket | P0 |
| CN-11 | Both threads close their connections | A | No leaked connections into the next test | P1 |
| CN-12 | A deadlock fails the test rather than hanging it | A | Every wait has a timeout | P1 |

**Cost.** CN-01 and CN-04 to CN-09 need `TransactionTestCase`, which truncates tables —
budget a few seconds each. CN-02, CN-03 and CN-10 run under plain `TestCase` at normal
speed. That is roughly eight slow tests in the whole suite, which is an acceptable
price for the only assurance that two calls cannot spend the same credit.

---

# 31. Migration data steps — `onboard/tests/test_migrations.py`

**What is untested today.** The suite builds its database by running every migration
against an *empty* one. So `0025`'s three `RunPython` steps execute — and process zero
rows. They are proven syntactically valid and nothing more.

**Why that matters.** Those steps run **exactly once**, against a real database with
real rows, and then never again. A bug in one silently corrupts data at the moment it
runs, and there is no second chance to notice. `0025` backfilled 35 subscriptions and
17 transactions on your dev database; `0026` then made a column `NOT NULL` on the
strength of that backfill having worked.

## The technique

Migrate *back* to the state before the migration under test, create rows using the
**historical** models, migrate forward, and assert what changed.

```python
class Migration0025Tests(TransactionTestCase):
    migrate_from = ("onboard", "0024_plan_renews_without_payment_and_more")
    migrate_to = ("onboard", "0025_subscription_canceled_at_and_more")

    def setUp(self):
        executor = MigrationExecutor(connection)
        executor.migrate([self.migrate_from])            # rewind
        old_apps = executor.loader.project_state([self.migrate_from]).apps

        # Build the "before" world with HISTORICAL models — the real ones have
        # save() overrides and constraints that did not exist yet, and using them
        # would test today's code rather than the migration.
        Subscription = old_apps.get_model("onboard", "Subscription")
        self.sub_id = Subscription.objects.create(end_date=None, ...).pk

        executor.loader.build_graph()                    # reload after rewinding
        executor.migrate([self.migrate_to])              # run the migration
        self.apps = executor.loader.project_state([self.migrate_to]).apps

    def test_end_date_backfilled(self):
        Subscription = self.apps.get_model("onboard", "Subscription")
        sub = Subscription.objects.get(pk=self.sub_id)
        self.assertIsNotNone(sub.end_date)
```

Three details that decide whether this works:

- **`TransactionTestCase`, not `TestCase`.** Migrations run DDL, which cannot happen
  inside a test transaction that will be rolled back.
- **Historical models only.** `apps.get_model()` from the executor's project state.
  Importing `onboard.models.Subscription` gives you *today's* model — with the `save()`
  override that fills `end_date`, which is precisely the behaviour the migration is
  supposed to be supplying. The test would pass while the migration did nothing.
- **Rebuild the graph after rewinding**, or the forward migrate uses a stale plan.

`django-test-migrations` wraps all of this in a `MigratorTestCase` and is worth the
dependency if these grow beyond a handful. The pattern is identical either way.

## Cases

| ID | Migration | Scenario | Expected | P |
| :--- | :--- | :--- | :--- | :--- |
| MG-01 | `0025` | NULL `end_date`, monthly plan | Backfilled to start plus one calendar month | P0 |
| MG-02 | `0025` | Yearly plan | Start plus one year | P0 |
| MG-03 | `0025` | `start_date` also null | Falls back to `created_at`, then now | P1 |
| MG-04 | `0025` | Existing `end_date` | **Left untouched** | P0 |
| MG-05 | `0025` | Snapshot without credit rules | Regenerated with all nine keys plus entitlements | P0 |
| MG-06 | `0025` | Snapshot that already has them | **Not rewritten** — it is a contract | P0 |
| MG-07 | `0025` | Tenant with 3 ACTIVE subscriptions | Newest by `start_date` kept; others EXPIRED with `canceled_at` and `superseded_by` | P0 |
| MG-08 | `0025` | Tie on `start_date` | Broken by `created_at`, deterministically | P1 |
| MG-09 | `0025` | Constraint applies after resolution | No IntegrityError on `AddConstraint` | P0 |
| MG-10 | `0025` | Transaction with a resolvable user | `tenant` populated from the mapping | P1 |
| MG-11 | `0025` | Transaction with no user | Left null; the check constraint still passes | P1 |
| MG-12 | `0025` | Run against already-clean data | No-op; nothing rewritten | P0 |
| MG-13 | `0026` | Straggler with NULL `end_date` created after `0025` | Filled before `NOT NULL` applies | P0 |
| MG-14 | `0026` | Column is `NOT NULL` afterwards | Insert with NULL fails | P0 |
| MG-15 | `wallet.0004` | One wallet per live tenant | Created; soft-deleted tenants skipped | P0 |
| MG-16 | `wallet.0004` | **No ledger row written** | An empty ledger sums to zero, which is the balance | P0 |
| MG-17 | `wallet.0004` | Tenant that already has a wallet | Not duplicated | P0 |
| MG-18 | `tenant.0009` / `0010` | New flags on existing rows | Default `False` — nobody is silently enforced | P0 |
| MG-19 | `wallet.0005` | Empty catalogue migration | No-op, no error | P2 |
| MG-20 | all | Full migrate from zero on an empty database | Succeeds; every data step handles zero rows | P0 |

> **MG-07 first.** That step edits live billing state, and on your dev database it
> happened to find nothing — so its resolution branch has **never actually run
> anywhere**. It is the only code in this cycle that has shipped completely
> unexercised.

---

# 32. Currency

**INR only, deliberately.** `Plan.CurrencyChoices` has a single member, every amount is
a `Decimal` of rupees, and the conversion to paise lives in one function.

| ID | Scenario | Expected | P |
| :--- | :--- | :--- | :--- |
| CU-01 | A plan's currency is INR | The only valid choice | P1 |
| CU-02 | Order amounts convert rupees to paise | Multiplied by 100, integer | P0 |
| CU-03 | Sub-paisa amounts | Quantised to 2 dp before conversion | P0 |
| CU-04 | Responses carry the currency | Present on every order | P2 |

Adding a second currency later means per-currency plans, a currency on the wallet or a
conversion at the boundary, and a decision about what a credit is worth in each. None of
that is designed, and writing tests as though it were would be misleading.
