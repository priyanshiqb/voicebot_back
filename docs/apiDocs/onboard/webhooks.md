# Razorpay Webhook API Documentation

The server-to-server callback Razorpay calls when a payment succeeds or fails. **You do not call
this endpoint** — it is documented so you can configure it, reason about it during an incident, and
understand why onboarding sometimes completes without the browser saying so.

- **Path**: `POST /api/v1/onboard/webhook/`
- **Caller**: Razorpay only.
- **Auth**: none in the usual sense — authenticity comes from the
  **`X-Razorpay-Signature`** header, an HMAC over the raw request body.

Its job is to be the **safety net**. The browser normally finishes onboarding by calling
[`/onboard/verify/`](./signup_flow.md#11-verify-the-payment), but a user who closes the tab after
paying never makes that call. This webhook completes the same work independently, so a paid
customer always ends up with an account.

**It now handles every kind of payment, not just signup.** A captured payment is dispatched on
`Transaction.transaction_type`:

| Type | Effect |
| :--- | :--- |
| `onboarding` | Promote the pending registrant, create the tenant, activate the subscription |
| `renewal` | Extend the cycle, re-stamp the rate card, grant the new allocation |
| `upgrade` | Apply the plan change, clearing any pending downgrade first |
| `top_up` | Deposit credits into the **purchased** bucket |
| `addon` | Grant the extra capacity that was paid for |

Before this dispatch existed, *every* captured payment ran the signup path — so a top-up would have
tried to promote a `TempUser` that does not exist.

## Idempotency

Razorpay retries aggressively, and a redelivered `payment.captured` used to re-run the whole
onboarding promotion. Two independent guards now prevent that:

1. **`RazorpayWebhookLog.event_id` is unique.** A duplicate event is recognised at insert and
   answered `200` without touching anything.
2. **The credit ledger enforces `(entry_type, reference_id)`.** A genuine redelivery carrying a
   *fresh* event id still cannot credit twice, because the grant is keyed on the order id.

## Response codes

| Status | When |
| :--- | :--- |
| `200` | Processed, already seen, unknown order, unhandled event type, **or processing failed** |
| `400` | Signature check failed |

**Failures return `200` deliberately.** A `500` makes Razorpay redeliver a payload that will fail
identically forever, and every redelivery is another chance to half-apply something. The error is
recorded on the webhook log row and the transaction is rolled back, so the event can be replayed by
hand once the cause is fixed. Check `RazorpayWebhookLog.error_message` when a payment cleared but
nothing happened.

## Configuration

| Setting | Purpose |
|---|---|
| `RAZORPAY_WEBHOOK_SECRET` | The shared secret used to verify `X-Razorpay-Signature`. Required whenever verification is on. |
| `RAZORPAY_KEY_ID` / `RAZORPAY_KEY_SECRET` | API credentials. **No defaults** — Django will not start without them. |
| `PAYMENTS_ALLOW_UNSIGNED` | Accept callbacks without checking their signature. Defaults to `False`. |

Point Razorpay at `https://<your-host>/api/v1/onboard/webhook/` and subscribe to
**`payment.captured`** and **`payment.failed`**.

> **Verification is skipped only when `PAYMENTS_ALLOW_UNSIGNED` is on**, which is correct for local
> work and required by the test suite. Any request is then accepted as genuine, so anyone who finds
> this URL could forge a `payment.captured`.
>
> It was previously inferred from `RAZORPAY_KEY_ID == "rzp_test_dummykey"` — which was also the
> settings default, so *forgetting* the credential silently disabled verification. Both the default
> and the inference are gone. Production settings now raise `ImproperlyConfigured` if the flag is
> on, and `manage.py check` reports `onboard.E001` whenever it is set outside `DEBUG`.

---

## Request

Razorpay's own envelope. The fields this endpoint reads:

| Field | Type | Description | Used for |
|---|---|---|---|
| `id` | string | Razorpay's event id. | Recorded on the audit log. |
| `event` | string | Event name. Only **`payment.captured`** and **`payment.failed`** are acted on. | Dispatch. |
| `payload.payment.entity.order_id` | string | The order this payment belongs to. | Matching the local transaction. |
| `payload.payment.entity.id` | string | The payment id. | Stored on the transaction (captured only). |

Header:

| Header | Description | Validation |
|---|---|---|
| `X-Razorpay-Signature` * | HMAC-SHA256 of the **raw body** using `RAZORPAY_WEBHOOK_SECRET`. | Verified against the raw bytes, not the parsed JSON. |

---

## What each event does

### `payment.captured`

If the matching transaction is **not already** `SUCCESSFUL`:

1. The transaction is marked `SUCCESSFUL` and the `razorpay_payment_id` recorded.
2. A `CustomUser` is created from the pending registration, carrying the password hash chosen at
   signup — so the customer's original password works immediately.
3. A `Tenant` is created from `business_name`, plus a `TenantUserMapping`.
4. An **active** `Subscription` is created for the selected plan.
5. The billing address and transaction are re-pointed at the real user.
6. The `TempUser` is marked onboarded and soft-deleted.

If the transaction is **already** `SUCCESSFUL` — the usual case, because the browser got there
first — nothing happens. That is what makes this safe to receive twice.

### `payment.failed`

The transaction is marked `FAILED` and the raw payload stored on it. Nothing else changes: the
`TempUser` survives, so the customer can retry from
[create-order](./signup_flow.md#10-create-the-order).

### Anything else

Logged and ignored. The response is still `200`.

---

## Responses

| Status | When | `message` | `errors` |
|---|---|---|---|
| **200** | Signature valid. The event was processed, or deliberately ignored. | `success` | `null` |
| **400** | Signature missing or invalid. **Nothing is processed.** | `unauthorized` | `["SIGNATURE_CHECK_FAILED"]` |
| **500** | The signature was valid but processing crashed. The failure is written to the audit log and then re-raised. | `Something went wrong` | `null` |

> **A `200` does not mean an account was created.** It means the event was received and handled —
> including "this order was already complete" and "we don't care about this event type". Do not
> infer state from the status code; read the transaction.

> **A `500` is deliberate.** The error is recorded on the webhook log first, then re-raised so
> Razorpay sees a failure and retries. Swallowing it would lose the payment.

---

## The audit log

**Every** request is written to `RazorpayWebhookLog` before anything else happens — including ones
that fail the signature check. Each row records the event id and type, the signature, whether it
verified, the full payload, and any processing error.

> **This is the first thing to read during a payment incident.** It is the only place a forged or
> malformed webhook leaves a trace, and it holds the exact payload Razorpay sent.

> **Log rows are never deduplicated.** Razorpay retries on non-2xx, so one payment can produce
> several entries. Correlate by `event_id`.

---

## Interaction with `/onboard/verify/`

Both paths complete the same work, and either can win.

| Scenario | What happens |
|---|---|
| Browser calls `/verify/` first | It completes onboarding. The webhook arrives, sees `SUCCESSFUL`, and does nothing. |
| Webhook arrives first | It completes onboarding. `/verify/` then finds a `SUCCESSFUL` transaction and issues tokens for the account that already exists. |
| Both arrive at once | `/verify/` takes a row lock on the transaction; the webhook does not. See [Known gaps](#known-gaps). |
| User closes the tab after paying | Only the webhook runs. The account exists, but **no JWTs are ever issued** — the customer must [log in](../user/auth.md#1-login) with the password they chose at signup. |

> **The webhook never returns tokens.** It has no browser to return them to. A customer onboarded
> purely by webhook has a working account and must sign in normally.

---

## Known gaps

| # | Gap | Impact |
|---|---|---|
| 1 | ~~The webhook path takes no database lock~~ **RESOLVED.** It now takes `select_for_update` on the transaction, the same row `/verify/` locks, so the two serialise. Whichever arrives second finds the order already successful — or the registrant already consumed — and returns without doing anything. | — |
| 2 | ~~Signature verification is skipped when `RAZORPAY_KEY_ID` is the dummy key~~ **RESOLVED.** Both the default and the inference are gone; verification is now an explicit `PAYMENTS_ALLOW_UNSIGNED` flag that defaults to off, guarded by production settings and `onboard.E001`. | — |
| 3 | **`payment.captured` does not verify the amount.** Only the `order_id` is matched. | A payment for less than the order total still completes onboarding. Applies to every payment type, not only signup — a short-paid top-up still deposits the full quantity. |
| 4 | **`payment.failed` does not notify anyone.** | A customer whose payment failed after closing the tab gets no email; they only discover it by returning to the signup flow. |
| 5 | **The `create_tenant_and_mapping` subdomain loop is not race-safe** — it queries for a free subdomain then creates. | Two simultaneous signups with the same business name can collide on the unique subdomain, producing a `500` and a retry. |

## Related

- [Signup flow](./signup_flow.md#11-verify-the-payment) — the browser-side completion path
- [Plans API](./plans.md) — where `total_price` comes from
- [Authentication API](../user/auth.md#1-login) — where webhook-onboarded users sign in
