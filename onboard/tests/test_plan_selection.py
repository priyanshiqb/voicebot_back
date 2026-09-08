"""Plan v2 — matching a quantity to a plan, and validating the ladder that makes it
possible.

Selection is only meaningful if ``minutes -> plan`` is a function, so these tests come
in two halves: that the right plan is picked, and that a catalogue which would make
the question unanswerable is caught before it can be activated.

Run with ``--settings=config.settings.test_postgres``.
"""

from decimal import Decimal

from django.test import TestCase

from onboard.models import Plan
from onboard.services import plan_selection
from onboard.tests.billing_factories import (
    DEFAULT_RATES,
    Operation,
    add_rates,
    make_minutes_plan,
    make_plan,
)

HIGH = Operation.CONVERSATION_HIGH_LATENCY
LOW = Operation.CONVERSATION_LOW_LATENCY
REEL = Operation.STATIC_REEL

FOR_MINUTES = "/api/v1/onboard/plans/for-minutes/"


def ranged_ladder():
    """The shape a continuous catalogue has: contiguous, open at both ends."""
    return [
        make_minutes_plan(min_minutes=None, max_minutes=500, sort_order=1,
                          minute_prices={HIGH: Decimal("2.75")}),
        make_minutes_plan(min_minutes=501, max_minutes=2000, sort_order=2,
                          minute_prices={HIGH: Decimal("1.80")}),
        make_minutes_plan(min_minutes=2001, max_minutes=None, sort_order=3,
                          minute_prices={HIGH: Decimal("1.20")}),
    ]


def pack_ladder():
    """The other shape: fixed quantities with gaps by construction."""
    return [
        make_minutes_plan(min_minutes=1000, max_minutes=1000, sort_order=1),
        make_minutes_plan(min_minutes=5000, max_minutes=5000, sort_order=2),
        make_minutes_plan(min_minutes=10000, max_minutes=10000, sort_order=3),
    ]


class LadderTests(TestCase):

    def test_ladder_is_ordered_open_bottom_first(self):
        small, mid, large = ranged_ladder()
        self.assertEqual(
            [p.pk for p in plan_selection.ladder()], [small.pk, mid.pk, large.pk]
        )

    def test_plans_without_a_basis_are_not_in_the_ladder(self):
        """A ladder is about to be checked for gaps, so a plan that cannot be sold
        must not look like coverage."""
        make_plan(credit_basis=None)
        minutes_plan = make_minutes_plan(min_minutes=None, max_minutes=None)

        self.assertEqual([p.pk for p in plan_selection.ladder()], [minutes_plan.pk])

    def test_a_basis_without_a_rupee_price_is_not_in_the_ladder(self):
        """It cannot be priced, so it must not sit in a ladder about to be checked
        for gaps — it would look like coverage that does not exist."""
        plan = make_plan(with_rates=False, credit_basis=HIGH,
                         min_minutes=1, max_minutes=500)
        add_rates(plan, {HIGH: 8})

        self.assertEqual(plan_selection.ladder(), [])

    def test_draft_and_retired_plans_are_excluded(self):
        make_minutes_plan(min_minutes=1, max_minutes=500, status=Plan.Status.DRAFT)
        make_minutes_plan(min_minutes=501, max_minutes=900, status=Plan.Status.RETIRED)

        self.assertEqual(plan_selection.ladder(), [])

    def test_ladders_are_separate_per_billing_cycle(self):
        monthly = make_minutes_plan(min_minutes=1, max_minutes=500)
        yearly = make_minutes_plan(
            min_minutes=1, max_minutes=500,
            billing_cycle=Plan.BillingCycleChoices.YEARLY,
        )

        self.assertEqual([p.pk for p in plan_selection.ladder()], [monthly.pk])
        self.assertEqual(
            [p.pk for p in plan_selection.ladder(
                billing_cycle=Plan.BillingCycleChoices.YEARLY)],
            [yearly.pk],
        )

    def test_ladder_does_not_query_per_plan(self):
        """The public endpoint reads the whole catalogue unauthenticated, so the
        rate rows are prefetched rather than fetched one plan at a time."""
        ranged_ladder()

        with self.assertNumQueries(2):
            plans = plan_selection.ladder()
            [p.is_minutes_based for p in plans]


class ForMinutesTests(TestCase):

    def test_exact_containment_on_a_ranged_ladder(self):
        small, mid, large = ranged_ladder()

        for minutes, expected in ((1, small), (500, small), (501, mid),
                                  (2000, mid), (2001, large), (99_999, large)):
            with self.subTest(minutes=minutes):
                plan, sold = plan_selection.for_minutes(minutes)
                self.assertEqual(plan.pk, expected.pk)
                self.assertEqual(sold, minutes)

    def test_a_contiguous_ladder_never_rounds_up(self):
        ranged_ladder()
        for minutes in (1, 250, 500, 501, 1200, 2000, 2001, 50_000):
            with self.subTest(minutes=minutes):
                _, sold = plan_selection.for_minutes(minutes)
                self.assertEqual(sold, minutes)

    def test_a_pack_ladder_rounds_up_to_the_smallest_covering_pack(self):
        small, mid, large = pack_ladder()

        plan, sold = plan_selection.for_minutes(1200)

        self.assertEqual(plan.pk, mid.pk)
        self.assertEqual(sold, 5000)

    def test_round_up_never_overshoots(self):
        """1,200 must reach the 5,000 pack, not the 10,000 one."""
        _, mid, large = pack_ladder()

        for minutes, expected, sold_expected in (
            (1, 1000, 1000), (1000, 1000, 1000), (1001, 5000, 5000),
            (5000, 5000, 5000), (5001, 10000, 10000), (10000, 10000, 10000),
        ):
            with self.subTest(minutes=minutes):
                plan, sold = plan_selection.for_minutes(minutes)
                self.assertEqual(plan.min_minutes, expected)
                self.assertEqual(sold, sold_expected)

    def test_above_the_largest_pack_nothing_matches(self):
        pack_ladder()

        self.assertEqual(plan_selection.for_minutes(10_001), (None, None))

    def test_packs_above_a_ranged_ladder_coexist(self):
        """Legal, because the pack sits where no ranged band claims territory."""
        make_minutes_plan(min_minutes=None, max_minutes=2000, sort_order=1)
        pack = make_minutes_plan(min_minutes=5000, max_minutes=5000, sort_order=2)

        self.assertEqual(plan_selection.for_minutes(1200)[1], 1200)
        self.assertEqual(plan_selection.for_minutes(5000)[0].pk, pack.pk)
        # Between the two, round-up reaches the pack.
        self.assertEqual(plan_selection.for_minutes(3000), (pack, 5000))

    def test_nothing_matches_an_empty_catalogue(self):
        self.assertEqual(plan_selection.for_minutes(1000), (None, None))

    def test_zero_and_negative_match_nothing(self):
        ranged_ladder()
        self.assertEqual(plan_selection.for_minutes(0), (None, None))
        self.assertEqual(plan_selection.for_minutes(None), (None, None))


class NearestBandsTests(TestCase):

    def test_reports_what_sits_either_side(self):
        pack_ladder()

        nearest = plan_selection.nearest_bands(7000)

        self.assertEqual(nearest["below"]["max_minutes"], 5000)
        self.assertEqual(nearest["above"]["min_minutes"], 10000)

    def test_above_the_top_there_is_nothing_above(self):
        pack_ladder()

        nearest = plan_selection.nearest_bands(20_000)

        self.assertEqual(nearest["below"]["max_minutes"], 10000)
        self.assertIsNone(nearest["above"])


class LadderProblemsTests(TestCase):

    def test_a_contiguous_ladder_is_clean(self):
        self.assertEqual(plan_selection.ladder_problems(ranged_ladder()), [])

    def test_a_pack_ladder_is_clean_despite_its_gaps(self):
        """Gaps between packs are the entire point of packs."""
        self.assertEqual(plan_selection.ladder_problems(pack_ladder()), [])

    def test_overlapping_bands_are_reported(self):
        plans = [
            make_minutes_plan(min_minutes=1, max_minutes=1000),
            make_minutes_plan(min_minutes=500, max_minutes=2000),
        ]
        codes = [p["code"] for p in plan_selection.ladder_problems(plans)]

        self.assertIn("BAND_OVERLAPS", codes)

    def test_an_open_ended_band_overlaps_everything_above_it(self):
        plans = [
            make_minutes_plan(min_minutes=2001, max_minutes=None),
            make_minutes_plan(min_minutes=5000, max_minutes=5000),
        ]
        codes = [p["code"] for p in plan_selection.ladder_problems(plans)]

        self.assertIn("BAND_OVERLAPS", codes)

    def test_a_gap_between_ranged_bands_is_reported(self):
        plans = [
            make_minutes_plan(min_minutes=None, max_minutes=500),
            make_minutes_plan(min_minutes=1000, max_minutes=None),
        ]
        problems = plan_selection.ladder_problems(plans)

        self.assertEqual([p["code"] for p in problems], ["BAND_GAP"])
        self.assertEqual(problems[0]["from_minutes"], 501)
        self.assertEqual(problems[0]["to_minutes"], 999)

    def test_adjacent_ranged_bands_are_not_a_gap(self):
        plans = [
            make_minutes_plan(min_minutes=None, max_minutes=500),
            make_minutes_plan(min_minutes=501, max_minutes=None),
        ]
        self.assertEqual(plan_selection.ladder_problems(plans), [])

    def test_a_gap_beside_a_pack_is_not_reported(self):
        plans = [
            make_minutes_plan(min_minutes=None, max_minutes=500),
            make_minutes_plan(min_minutes=5000, max_minutes=5000),
        ]
        self.assertEqual(plan_selection.ladder_problems(plans), [])


class LadderWarningsTests(TestCase):

    def test_a_full_ranged_ladder_warns_about_nothing(self):
        self.assertEqual(plan_selection.ladder_warnings(ranged_ladder()), [])

    def test_missing_open_bottom_and_top_are_warned(self):
        plans = [make_minutes_plan(min_minutes=100, max_minutes=500)]
        codes = [w["code"] for w in plan_selection.ladder_warnings(plans)]

        self.assertIn("LADDER_OPEN_BOTTOM_MISSING", codes)
        self.assertIn("LADDER_OPEN_TOP_MISSING", codes)

    def test_a_volume_ladder_is_never_warned_about_its_boundaries(self):
        """A cliff at every boundary is what a volume discount *is*, so warning about
        it would fire on every legitimate ladder — see ``boundary_prices``."""
        plans = [
            make_minutes_plan(min_minutes=None, max_minutes=500,
                              minute_prices={HIGH: Decimal("2.75")},
                              discount_percentage=Decimal("0.00")),
            make_minutes_plan(min_minutes=501, max_minutes=None,
                              minute_prices={HIGH: Decimal("1.80")},
                              discount_percentage=Decimal("0.00")),
        ]
        self.assertEqual(plan_selection.ladder_warnings(plans), [])


class BoundaryPricesTests(TestCase):
    """What a customer pays either side of a boundary — reported, never judged."""

    def test_reports_both_sides_of_each_boundary(self):
        plans = [
            make_minutes_plan(min_minutes=None, max_minutes=500,
                              minute_prices={HIGH: Decimal("2.75")},
                              discount_percentage=Decimal("0.00"),
                              tax_percentage=Decimal("0.00")),
            make_minutes_plan(min_minutes=501, max_minutes=None,
                              minute_prices={HIGH: Decimal("1.80")},
                              discount_percentage=Decimal("0.00"),
                              tax_percentage=Decimal("0.00")),
        ]
        rows = plan_selection.boundary_prices(plans)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["at_minutes"], 500)
        self.assertEqual(rows[0]["price_below"], "1375.00")
        self.assertEqual(rows[0]["next_minutes"], 501)
        self.assertEqual(rows[0]["price_above"], "901.80")
        self.assertTrue(rows[0]["cheaper_above"])

    def test_a_ladder_with_no_volume_discount_is_not_cheaper_above(self):
        plans = [
            make_minutes_plan(min_minutes=None, max_minutes=500,
                              minute_prices={HIGH: Decimal("1.80")},
                              discount_percentage=Decimal("0.00")),
            make_minutes_plan(min_minutes=501, max_minutes=None,
                              minute_prices={HIGH: Decimal("1.80")},
                              discount_percentage=Decimal("0.00")),
        ]
        self.assertFalse(plan_selection.boundary_prices(plans)[0]["cheaper_above"])

    def test_open_ended_boundaries_are_skipped(self):
        plans = [make_minutes_plan(min_minutes=None, max_minutes=None)]
        self.assertEqual(plan_selection.boundary_prices(plans), [])


class RupeesPerCreditTests(TestCase):

    def test_a_consistent_plan_prices_credits_the_same_on_every_tier(self):
        plan = make_minutes_plan()
        ratios = plan_selection.rupees_per_credit(plan)

        self.assertEqual(len(set(ratios.values())), 1)

    def test_an_inconsistent_plan_is_visible(self):
        plan = make_minutes_plan(minute_prices={REEL: Decimal("0.10")})
        ratios = plan_selection.rupees_per_credit(plan)

        self.assertGreater(len(set(ratios.values())), 1)

    def test_tiers_without_both_numbers_are_omitted(self):
        """Nothing can be concluded from them, so they are absent rather than zero."""
        plan = make_plan(with_rates=False, credit_basis=None)
        add_rates(plan, {REEL: 4, HIGH: 8, LOW: 12})   # credits, no rupee prices

        self.assertEqual(plan_selection.rupees_per_credit(plan), {})


class ForMinutesEndpointTests(TestCase):
    """The public contract the frontend is built against."""

    def test_returns_the_plan_its_price_and_its_credits(self):
        ranged_ladder()

        response = self.client.get(FOR_MINUTES, {"minutes": 1200})
        body = response.json()["data"]

        self.assertEqual(response.status_code, 200)
        self.assertEqual(body["requested_minutes"], 1200)
        self.assertEqual(body["selected_minutes"], 1200)
        self.assertFalse(body["rounded_up"])
        self.assertEqual(body["money"]["net"], "2160.00")
        self.assertEqual(body["money"]["total"], "2548.80")
        self.assertEqual(body["credits"]["granted"], 1200 * DEFAULT_RATES[HIGH])

    def test_equivalent_minutes_are_reported_for_every_tier(self):
        """The grant is only worth the promised minutes on the basis tier, and the
        customer has to be able to see that."""
        ranged_ladder()

        body = self.client.get(FOR_MINUTES, {"minutes": 1200}).json()["data"]
        equivalents = body["credits"]["equivalent_minutes"]

        self.assertEqual(equivalents[HIGH], 1200)
        self.assertEqual(equivalents[REEL], 1200 * DEFAULT_RATES[HIGH] // DEFAULT_RATES[REEL])
        self.assertEqual(equivalents[LOW], 1200 * DEFAULT_RATES[HIGH] // DEFAULT_RATES[LOW])

    def test_a_rounded_up_request_says_so(self):
        pack_ladder()

        body = self.client.get(FOR_MINUTES, {"minutes": 1200}).json()["data"]

        self.assertEqual(body["requested_minutes"], 1200)
        self.assertEqual(body["selected_minutes"], 5000)
        self.assertTrue(body["rounded_up"])
        self.assertTrue(body["plan"]["is_fixed_quantity"])

    def test_price_and_credits_follow_the_sold_quantity_not_the_request(self):
        """The failure that would charge for a pack and grant the smaller amount."""
        pack_ladder()

        body = self.client.get(FOR_MINUTES, {"minutes": 1200}).json()["data"]

        self.assertEqual(body["credits"]["granted"], 5000 * DEFAULT_RATES[HIGH])
        self.assertEqual(
            Decimal(body["money"]["net"]),
            Decimal(body["money"]["price_per_minute_inr"]) * 5000,
        )

    def test_unmatched_quantity_returns_the_nearest_bands(self):
        pack_ladder()

        response = self.client.get(FOR_MINUTES, {"minutes": 20_000})

        self.assertEqual(response.status_code, 404)
        data = response.json()["data"]
        self.assertEqual(data["below"]["max_minutes"], 10000)
        self.assertIsNone(data["above"])

    def test_a_missing_or_nonsensical_quantity_is_a_400(self):
        ranged_ladder()

        for params in ({}, {"minutes": "abc"}, {"minutes": 0}, {"minutes": -5},
                       {"minutes": 10_000_001}):
            with self.subTest(params=params):
                self.assertEqual(self.client.get(FOR_MINUTES, params).status_code, 400)

    def test_the_endpoint_is_public(self):
        """It is the pricing page, so it has to answer an anonymous caller."""
        ranged_ladder()
        self.assertEqual(self.client.get(FOR_MINUTES, {"minutes": 100}).status_code, 200)

    def test_yearly_ladder_is_addressable(self):
        make_minutes_plan(min_minutes=None, max_minutes=None,
                          billing_cycle=Plan.BillingCycleChoices.YEARLY)

        matched = self.client.get(
            FOR_MINUTES, {"minutes": 12_000, "billing_cycle": "yearly"}
        )
        unmatched = self.client.get(FOR_MINUTES, {"minutes": 12_000})

        self.assertEqual(matched.status_code, 200)
        self.assertEqual(unmatched.status_code, 404)
