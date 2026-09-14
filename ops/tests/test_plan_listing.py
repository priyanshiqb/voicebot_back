"""`GET /api/v1/ops/plans/` — filters, search, ordering and pagination.

The list used to serialise every plan and never call `filter_queryset`, so `status`,
`billing_cycle`, `page` and `page_size` were accepted and silently ignored: the console
asked for retired yearly plans and got the whole catalogue back.

Filtering is declarative (`filterset_fields` / `search_fields` / `ordering_fields` on
the viewset). Assertions are made against the plans each test creates rather than
absolute counts, so a plan seeded by a data migration cannot make them flaky.
"""

from django.test import TestCase

from onboard.models import Plan
from onboard.tests.billing_factories import make_plan
from ops.tests.test_plan_api import PLANS, client_for, operator

Status = Plan.Status
Cycle = Plan.BillingCycleChoices


class PlanListFilterTests(TestCase):
    def setUp(self):
        self.client = client_for(operator("filters@ops.com"))

        self.monthly_active = make_plan("monthly-active")
        self.yearly_active = make_plan("yearly-active", billing_cycle=Cycle.YEARLY)
        self.yearly_retired = make_plan(
            "yearly-retired", billing_cycle=Cycle.YEARLY, status=Status.RETIRED
        )
        self.monthly_retired = make_plan("monthly-retired", status=Status.RETIRED)
        self.monthly_draft = make_plan("monthly-draft", status=Status.DRAFT)

        self.mine = {
            str(plan.pk)
            for plan in (
                self.monthly_active, self.yearly_active, self.yearly_retired,
                self.monthly_retired, self.monthly_draft,
            )
        }

    def _rows(self, **params):
        params.setdefault("page_size", 100)
        response = self.client.get(PLANS, params)
        self.assertEqual(response.status_code, 200)
        return response.data["data"]

    def _ids(self, **params):
        """Which of *this test's* plans came back."""
        return {row["id"] for row in self._rows(**params)} & self.mine

    def test_the_reported_request_returns_only_retired_yearly_plans(self):
        """?status=retired&billing_cycle=yearly, exactly as the console sends it."""
        rows = self._rows(status="retired", billing_cycle="yearly")

        self.assertEqual({r["id"] for r in rows} & self.mine, {str(self.yearly_retired.pk)})
        # Every row returned satisfies both filters — including any plan this test
        # did not create.
        for row in rows:
            self.assertEqual(row["status"], "retired")
            self.assertEqual(row["billing_cycle"], "yearly")

    def test_status_alone(self):
        self.assertEqual(
            self._ids(status="retired"),
            {str(self.yearly_retired.pk), str(self.monthly_retired.pk)},
        )

    def test_billing_cycle_alone(self):
        self.assertEqual(
            self._ids(billing_cycle="yearly"),
            {str(self.yearly_active.pk), str(self.yearly_retired.pk)},
        )

    def test_drafts_are_filterable(self):
        self.assertEqual(self._ids(status="draft"), {str(self.monthly_draft.pk)})

    def test_without_filters_every_status_is_listed(self):
        """The documented default: drafts and retired plans included."""
        self.assertEqual(self._ids(), self.mine)

    def test_an_unrecognised_value_is_a_400(self):
        """
        A ChoiceFilter is strict. A typo is refused rather than quietly returning the
        whole catalogue, which would look like a real answer to the console.
        """
        response = self.client.get(PLANS, {"status": "retird"})
        self.assertEqual(response.status_code, 400)

    def test_filter_values_are_case_sensitive(self):
        """Choice values are lower-case, and `RETIRED` is not one of them."""
        response = self.client.get(PLANS, {"status": "RETIRED"})
        self.assertEqual(response.status_code, 400)

    def test_search_matches_name_or_code(self):
        self.assertEqual(self._ids(search="yearly-ret"), {str(self.yearly_retired.pk)})

    def test_search_composes_with_filters(self):
        self.assertEqual(
            self._ids(search="monthly", status="retired"),
            {str(self.monthly_retired.pk)},
        )


class PlanListOrderingTests(TestCase):
    def setUp(self):
        self.client = client_for(operator("ordering@ops.com"))
        self.plans = [make_plan(f"ordered-{index}", sort_order=1) for index in range(3)]
        self.mine = [str(plan.pk) for plan in self.plans]

    def _order(self, ordering):
        response = self.client.get(PLANS, {"ordering": ordering, "page_size": 100})
        self.assertEqual(response.status_code, 200)
        return [row["id"] for row in response.data["data"] if row["id"] in self.mine]

    def test_created_at_both_directions(self):
        ascending = self._order("created_at")
        self.assertEqual(ascending, self.mine)
        self.assertEqual(self._order("-created_at"), list(reversed(ascending)))

    def test_an_unknown_ordering_key_falls_back_to_the_default(self):
        response = self.client.get(PLANS, {"ordering": "not_a_column"})
        self.assertEqual(response.status_code, 200)


class PlanListPaginationTests(TestCase):
    def setUp(self):
        self.client = client_for(operator("pages@ops.com"))
        # Same sort_order on purpose: most real plans share the default, and a tie
        # without a unique final ordering key is how two pages end up overlapping.
        for index in range(5):
            make_plan(f"paged-{index}", sort_order=1)

    def test_page_size_is_honoured_and_meta_is_present(self):
        response = self.client.get(PLANS, {"page": 1, "page_size": 2})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data["data"]), 2)
        meta = response.data["meta"]
        self.assertGreaterEqual(meta["count"], 5)
        self.assertEqual(meta["page_size"], 2)

    def test_paging_covers_every_plan_exactly_once(self):
        everything = [
            row["id"]
            for row in self.client.get(PLANS, {"page_size": 100}).data["data"]
        ]

        seen = []
        page = 1
        while True:
            response = self.client.get(PLANS, {"page": page, "page_size": 2})
            seen.extend(row["id"] for row in response.data["data"])
            if not response.data["meta"]["next"]:
                break
            page += 1

        self.assertEqual(len(seen), len(set(seen)), "a plan appeared on two pages")
        self.assertEqual(set(seen), set(everything))

    def test_filters_and_pagination_compose(self):
        make_plan("paged-retired-a", status=Status.RETIRED)
        make_plan("paged-retired-b", status=Status.RETIRED)

        response = self.client.get(
            PLANS, {"status": "retired", "page": 1, "page_size": 1}
        )

        self.assertEqual(len(response.data["data"]), 1)
        # The count is of the filtered set, not the catalogue.
        self.assertEqual(
            response.data["meta"]["count"],
            Plan.objects.filter(status=Status.RETIRED, is_deleted=False).count(),
        )
