# Wallet API Documentation

The tenant's credit balance, what a call costs them, and where every credit went.

**Base path:** `/api/v1/billing/wallet/`

Three things worth knowing before you build against this:

- **The ledger is the truth; the balance is a cache.** `SUM(ledger.amount)` per wallet
  *is* the balance, and a weekly job asserts it. Nothing outside one service is allowed
  to move either.
- **Prices come from the wallet, not the plan.** The rate card is copied onto the
  wallet when a subscription activates or changes. What a tenant pays is what was
  stamped, which may differ from what the pricing page advertises today.
- **Nothing here is writable.** There is no endpoint that adds or removes credits.
  Credits arrive from a cycle allocation, a paid top-up, or a staff adjustment; they
  leave through usage or a forfeit. All of it is recorded.

## Endpoints Summary

- `GET /api/v1/billing/wallet/` — Balance and spendability
- `GET /api/v1/billing/wallet/rates/` — What each call tier costs *this* tenant
- `POST /api/v1/billing/wallet/quote/` — What a given quantity would cost
- `GET /api/v1/billing/wallet/ledger/` — Every movement, newest first

---

## Two buckets, one balance

`balance` is always `allocated_balance + purchased_balance`, enforced by a database
constraint. The split is exposed because it is something the tenant can act on:

| Bucket | Where it came from | What happens at renewal |
|---|---|---|
| `allocated` | The plan's cycle allocation | May be forfeited — depends on the plan's rollover rules |
| `purchased` | A cash top-up | **Always survives.** Never expires, never forfeited |

Consumption drains `allocated` first, so the credits a tenant paid for directly are the
ones that last. Hiding the split would make "why did my balance drop at renewal?"
unanswerable from the API.

---

## `GET /billing/wallet/`

```json
{
  "status": true,
  "message": "Wallet balance retrieved",
  "data": {
    "balance": 1030,
    "allocated_balance": 980,
    "purchased_balance": 50,
    "is_frozen": false,
    "frozen_reason": null,
    "frozen_at": null,
    "is_spendable": true,
    "is_low": false,
    "low_balance_threshold": 100,
    "plan_code": "starter_inr",
    "plan_name": "Starter INR",
    "lifetime_granted": 1050,
    "lifetime_consumed": 20,
    "updated_at": "2026-08-24T09:14:22Z"
  }
}
```

**`is_frozen`** blocks spending, not granting — a renewal payment must be able to land
in a frozen wallet and unfreeze it. A wallet freezes when a cycle lapses unpaid, or by
a support action.

**`lifetime_*`** are analytics only and never used to decide anything.

**No subscription is required.** The wallet is anchored to the tenant, not to a
subscription — it outlives every plan change, and this endpoint keeps answering for a
lapsed tenant. It is where they see that their purchased credits are still there.

---

## `GET /billing/wallet/rates/`

What this tenant pays per minute, read from the rate card stamped on their wallet.

```json
{
  "data": {
    "plan_code": "starter_inr",
    "stamped_at": "2026-08-01T00:00:00Z",
    "operations": [
      { "operation": "conversation_high_latency", "label": "Conversation — Standard (high latency)",
        "unit": "per_minute", "credits_per_unit": 5, "rounding": "ceil", "minimum_units": 1 },
      { "operation": "conversation_low_latency",  "label": "Conversation — Realtime (low latency)",
        "unit": "per_minute", "credits_per_unit": 10, "rounding": "ceil", "minimum_units": 1 },
      { "operation": "static_reel",               "label": "Static Reel (one-way playback)",
        "unit": "per_minute", "credits_per_unit": 2, "rounding": "ceil", "minimum_units": 1 }
    ]
  }
}
```

An empty `operations` list means no rate card has been stamped — calls for that tenant
would fail to price. It is a configuration fault, not a valid state.

---

## `POST /billing/wallet/quote/`

What something would cost, and whether the balance covers it. Calculates only —
nothing is reserved, deducted or recorded.

**Request**

```json
{ "operation": "conversation_low_latency", "quantity": 90 }
```

`quantity` is in the operation's natural unit — **seconds**, for a call.

**Response**

```json
{
  "data": {
    "operation": "conversation_low_latency",
    "credits": 20,
    "units": 2,
    "raw_quantity": 90,
    "unit_type": "per_minute",
    "rate_applied": 10,
    "affordable": true,
    "balance": 1030
  }
}
```

The quote and the eventual charge come from the same calculation, so the number shown
to a tenant is the number they are charged.

| Status | When |
|---|---|
| `400` | `quantity` below 1, or an `operation` that is not one of the three tiers |
| `409 RATE_NOT_CONFIGURED` | A valid tier the tenant's plan has no price for |

**The two are different failures and are reported differently on purpose.** A typo is
the caller's mistake; "this plan does not price that tier" is the platform's state, and
is actionable by whoever configures plans. A client that could not tell them apart
would send someone to the plan configuration to chase a spelling error.

---

## `GET /billing/wallet/ledger/`

Every credit movement, newest first. Paginated.

```json
{
  "data": [
    {
      "id": "…",
      "entry_type": "usage_deduction",
      "entry_type_display": "Usage Deduction",
      "bucket": "allocated",
      "amount": -20,
      "balance_after": 1030,
      "operation": "conversation_low_latency",
      "rating": {
        "raw_quantity": 90, "units": 2,
        "unit_type": "per_minute", "rate_applied": 10
      },
      "reference_id": "call_9fA…",
      "notes": null,
      "created_at": "2026-08-24T09:14:22Z"
    }
  ],
  "meta": { "count": 42, "next": "…", "previous": null, "page": 1, "page_size": 10 }
}
```

> **Pagination shape.** Rows come back directly in `data`, with pagination under
> `meta`. There is no `data.results`.

**`rating` is the point of this endpoint.** It answers "why was I charged 20 credits?"
without a support ticket — and it stays correct after a price change, because the
numbers were copied onto the row rather than looked up now. It is `null` on rows that
are not usage.

### Filters

| Parameter | Example |
|---|---|
| `entry_type` | `?entry_type=usage_deduction` |
| `bucket` | `?bucket=purchased` |
| `operation` | `?operation=conversation_low_latency` |
| `is_credit` | `?is_credit=true` — additions only |
| `created_after` / `created_before` | `?created_after=2026-08-01T00:00:00Z` |
| `search` | Matches `reference_id`, `notes`, `operation` |
| `ordering` | `created_at`, `amount`, `balance_after` |

### Entry types

| Credits in | Credits out |
|---|---|
| `opening_balance` | `usage_deduction` |
| `cycle_allocation` | `usage_adjustment` |
| `manual_top_up` | `rollover_forfeit` |
| `usage_refund` | `rollover_cap_forfeit` |
| `admin_adjustment` *(either sign)* | `expiry_forfeit` |

`rollover_forfeit` and `rollover_cap_forfeit` are separate on purpose: "your plan does
not roll over" and "you hit the cap" are two different conversations.

**A cross-bucket charge writes two rows.** Spending 15 credits with 5 allocated and 50
purchased produces `allocated: -5` and `purchased: -10`, the second with a `#purchased`
suffix on its reference. One row claiming `bucket: allocated` for credits partly drawn
from elsewhere would be a false statement in a ledger whose whole job is explaining the
balance.

### Write attempts

`POST`, `PUT`, `PATCH` and `DELETE` all return **405**. The ledger is append-only — a
correction is a compensating `admin_adjustment`, never an edit.
