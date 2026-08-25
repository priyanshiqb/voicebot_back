# Design Your Plans — What Commercial Can Configure

**For:** commercial decision-makers
**From:** engineering
**Status:** the billing system is built and tested. It ships with **no commercial numbers
of its own** — every price, allowance and limit is yours to set.

> **There is no fixed set of plans.** You decide how many there are, what they are
> called, what each includes and what each costs. Everything on this page is entered by
> an administrator through a screen — none of it needs engineering.

A shareable version of this document is published separately; ask engineering for the
link if you need one for a meeting.

---

## 1. We meter two things, and they behave nothing alike

Every setting below belongs to one or the other. Confusing them is how subscription
businesses end up with support tickets nobody can answer.

| | **Credits** | **Capacity** |
| :--- | :--- | :--- |
| What it is | Talk time | Agents, seats, contacts, knowledge bases, crawled pages, storage, campaigns |
| Behaves like | Petrol — spent and gone | A parking space — occupied, then released |
| Customer sees | A balance going down | A limit they are under or over |
| You price it in | Credits per minute | **Rupees per billing cycle** |
| The test | Hang up a call — the minutes do *not* come back | Delete a file — the storage *does* come back |

That test is why storage is sold as capacity rather than "credits per gigabyte". If it
were credits, deleting a file would raise a refund question with no good answer: refund
the unused days and a customer can churn uploads to game us; refuse, and they paid for
storage they gave back.

---

## 2. What you set on every plan

Eleven settings. Every plan gets its own values for all of them, so two plans can differ
on any dimension you like — not just price.

### 2.1 Price — three numbers

You set the list price and two percentages; the system derives what is charged, and that
figure is what goes to the payment gateway.

```
list price      ₹2,999
− discount 10%  ₹2,699.10
+ tax 18%       ₹3,184.94  ← charged
```

A standing discount is a presentation choice. "₹2,999, 10% off" reads differently from
"₹2,699" — but a discount that never expires is really just the price.

### 2.2 Billing cycle — monthly or yearly

A yearly plan is a **separate plan**, not a toggle, so it can have different allowances
as well as a different price.

> **Example.** Price a yearly plan at ten times the monthly price and grant twelve
> months of credits: the customer pays for ten months and gets twelve — a ~17% saving —
> and you take a year of revenue up front.

*Weekly and quarterly billing are not available. Adding one is engineering work.*

### 2.3 Credits included per cycle

> **Example.** 1,000 credits at 5 credits a minute is **200 minutes a month**. The same
> 1,000 at 3 a minute is **333 minutes**. The credit figure alone means nothing to a
> customer; the minutes do.

### 2.4 Rate card — what a minute costs, three ways

Every plan prices all three ways of running a call. This is a **cost** choice, not a
feature gate — a customer on any plan can use any of them.

| Tier | What it is |
| :--- | :--- |
| Static reel | One-way playback, no conversation |
| Standard conversation | The normal conversational model |
| Realtime conversation | The low-latency model |

> **Example.** Set realtime at double standard to reflect what it costs to run, and make
> a higher plan cheaper per minute as well as more generous — so moving up pays a
> customer twice.

### 2.5 How a call is measured — per tier

| Setting | Options |
| :--- | :--- |
| Unit | Per minute · per second · **per call** (flat, duration ignored) |
| Rounding | Up · down · to nearest |
| Minimum charge | Smallest billable quantity, e.g. always bill at least one minute |

> **Example.** Per minute, rounded up, minimum 1 — the telecoms standard — bills a
> 90-second call as two minutes and a 4-second call as one. Rounding *down* would make
> that 4-second call free.

A flat per-call price is worth considering for the reel tier, where the recording fixes
the duration anyway.

### 2.6 Top-up price — rupees per credit

**Setting it to zero disables top-ups entirely**, making an upgrade the only way to get
more.

> **Example.** If a plan gives 1,000 credits for ₹999, an included credit is worth ₹1.
> Price the top-up at ₹1.20 and buying extra carries a 20% premium — the incentive to be
> on the right plan rather than living on top-ups. Too low and everyone stays on the
> cheapest plan; too high and growth feels punished.

### 2.7 Rollover — three switches and a cap

Independent switches at **renewal**, at an **upgrade**, at a **downgrade**, plus a
ceiling on how much can carry.

> **Example.** A customer uses 400 of 1,000 credits and renews. Rollover off: the 600
> are burned and they restart at 1,000. Rollover on, capped at 1,000: they carry all 600
> and start at 1,600.

⚠️ **Think carefully about this on a yearly plan.** A year's credits are granted in a
single block on day one, so switching rollover off can burn twelve months of unused
credits on the day the customer pays you again.

### 2.8 After a cycle lapses — on or off

Off (the normal case): the plan's credits are forfeited and the account stops making
calls until they renew. On: the subscription ends but the balance stays spendable — how
you would build a pure pay-as-you-go product.

**Credits bought with cash are never confiscated either way.** They are held, and become
spendable again the moment a payment clears.

### 2.9 Free renewal — zero-priced plans only

This single switch is the difference between a **30-day trial** and a **permanent free
tier**. Off: the plan lapses after a month. On: the cycle rolls over for nothing,
forever, with a fresh credit grant each time.

### 2.10 Low-balance warning

We notify the customer once their balance falls to this number; zero switches it off.

> **Example.** On a monthly plan, 10% of the allowance gives a few days' notice. **On a
> yearly plan, 10% of a year arrives with about five weeks left** — so a percentage that
> works monthly is probably the wrong figure annually.

### 2.11 Refill period — per resource

Most limits are a **standing total**: three agents means three agents, and deleting one
gives the seat back. That is the default, and it is right for anything a customer
*holds*.

Give one a period instead and it becomes an allowance that refills. What is limited is
then the **act**, counted from when it happened — so removing the record afterwards does
not give it back. Campaigns work this way: the calls went out whether or not the
campaign still exists.

Six periods: **daily, weekly, monthly, quarterly, half-yearly, yearly.**

> **Example.** *Monthly* makes it a quota — ten campaigns a month. *Daily* on the same
> resource makes it a pace — three a day, so nobody can fire a month's worth in an
> afternoon. Same setting, completely different product behaviour.

**The period is half of what the number means** — ten a month and ten a year are the
same figure and very different offers — so it is frozen onto each customer's
subscription alongside the figure. Changing a plan's period never re-reads an allowance
somebody is already living under.

*Windows run from each customer's own start date rather than the calendar, so an
allowance never straddles a billing cycle.*

---

## 3. What you set for each of the seven countable resources

All six values, **per resource, per plan** — so storage can be generous on one plan and
tight on another, and purchasable on one and not the other.

| Setting | What it does | Example |
| :--- | :--- | ---: |
| Included | How many the plan grants. Can be **unlimited** | 10 GB |
| How often it refills | **What the number means.** Never (a standing total), or daily → yearly | Never |
| Can they buy more? | If no, upgrading is the only route | Yes |
| Price per block | **Recurring every cycle**, not a one-off | ₹199 per cycle |
| Block size | Sold in blocks, rounded up — 12 GB bills two 10 GB blocks | 10 GB |
| Ceiling | Most they may add, or uncapped | +90 GB |

The seven resources: voicebot agents · user seats · contacts · knowledge bases · crawled
pages · storage · campaigns.

**Extras are rent, not a purchase.** A customer who buys 20 GB pays for it again at every
renewal until they remove it. Buying mid-cycle is prorated.

**The ceiling is what protects your tiers.** Set each below the next plan's included
amount and a customer cannot assemble a bigger plan at add-on prices.

> ⚠️ **Block prices are per billing cycle — so a yearly plan needs yearly add-on prices.**
>
> Enter ₹199 per 10 GB on a monthly plan and the customer pays ₹199 a month. Enter the
> same ₹199 on a *yearly* plan and they pay ₹199 **for the whole year** — the same
> storage at a twelfth of the price.
>
> If you want 10 GB to be worth ₹199 a month, the yearly figure is ₹2,388 — or ₹1,990 if
> add-ons should carry the same annual discount as the plan. That is a decision, not a
> conversion.

**One exception:** campaigns cannot currently be *bought* as an add-on — a customer at
their limit must upgrade. It is the one resource where prorated add-on pricing misprices
badly, because it is a count inside a window rather than a quantity held over time: a
customer buying late in the window would pay a fraction of the price for the whole
allowance. Selling it needs flat pricing, which is a small change but has to be asked
for. How often the allowance *refills* is entirely yours to set.

---

## 4. And how they are presented

| Setting | What it does |
| :--- | :--- |
| Name & description | What appears on the pricing page |
| Display order | Left-to-right order of the plans |
| "Most popular" badge | Can be put on any plan — or several |
| Active | Switch a plan off and it stops being offered. Existing customers are untouched |

---

## 5. The worksheet

Add or remove plan columns as you like — there is no limit on how many plans you define,
and a yearly version of a plan is simply another column.

| Setting | Plan A | Plan B | Plan C | Plan D |
| :--- | :--- | :--- | :--- | :--- |
| Plan name | | | | |
| List price | | | | |
| Discount % | | | | |
| Tax % | | | | |
| Billing cycle | | | | |
| Credits included | | | | |
| Credits / min — reel | | | | |
| Credits / min — standard | | | | |
| Credits / min — realtime | | | | |
| Top-up ₹ per credit | | | | |
| Roll over at renewal? | | | | |
| Roll over at upgrade? | | | | |
| Roll over at downgrade? | | | | |
| Rollover ceiling | | | | |
| Credits usable after lapse? | | | | |
| Renews free? *(₹0 plans)* | | | | |
| Low-balance warning at | | | | |
| Agents · included / +block / ₹ / ceiling | | | | |
| Seats · included / +block / ₹ / ceiling | | | | |
| Contacts · included / +block / ₹ / ceiling | | | | |
| Knowledge bases · included / +block / ₹ / ceiling | | | | |
| Crawled pages · included / +block / ₹ / ceiling | | | | |
| Storage · included / +block / ₹ / ceiling | | | | |
| Campaigns · included | | | | |
| Campaigns · refills how often | | | | |

---

## 6. What is fixed, and would need engineering

| Fixed | Why it matters to you |
| :--- | :--- |
| Indian rupees only | Another currency is a design question, not a setting — it needs a decision about what a credit is worth in each |
| Monthly or yearly **billing** cycles | No weekly or quarterly option for *charging*. Allowance refill periods are separate and go down to daily |
| Three call tiers | Reel, standard, realtime. You price them; you cannot add a fourth |
| Seven countable resources | You set every limit on them; adding an eighth kind of thing to count is engineering work |
| Campaigns cannot be bought as an add-on | Needs flat pricing rather than prorated — a small change, if you want it |

---

## 7. Two things worth knowing before you set numbers

**Nothing blocks anyone yet.** Credits and limits are metered and recorded from day one,
but *refusing* to serve a customer is switched on per customer — and it is currently off
for everyone. That is deliberate: we can run a full month of real traffic, compare what
customers actually burned against what they paid, correct the numbers, and only then
start enforcing. **A wrong number today is recoverable. A wrong number that is also being
enforced is not.**

**Changing a price never affects an existing customer mid-cycle.** A customer's terms —
price, credits, limits, per-minute rates, and now refill periods — are frozen when they
subscribe and re-frozen at each renewal. Editing a plan changes what *new and renewing*
customers get. Nobody's bill changes under them, and nobody's limits shrink overnight.

---

## 8. What we need from you

1. **How many plans, and what are they for?** Decide the shape — who each plan is aimed
   at — before the numbers.
2. **The worksheet, filled in.** Every row, for every plan.
3. **The cost of a minute on each of the three tiers**, or who knows it. The rate card
   cannot be set sensibly without it, and it is the single figure that decides margin on
   every call we ever make.

Once the worksheet comes back, an administrator enters it and we run a metering month
before anything starts being refused.
