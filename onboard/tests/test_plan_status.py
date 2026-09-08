"""`Plan.status` — the field that stops a plan being sold before it is finished.

Two guarantees, and the second is the one that would fail quietly:

* **A new plan is a draft**, so it cannot be sold with no entitlements and no rates.
* **`is_active` follows `status`**, everywhere, including through the inherited
  `activate()` / `deactivate()` that write `is_active` on its own.
"""

from decimal import Decimal

from django.test import TestCase

from onboard.models import Plan

Status = Plan.Status


def make_plan(code, **kwargs):
    return Plan.objects.create(
        code=code, name=code, **kwargs
    )


class PlanStatusTests(TestCase):
    def test_a_new_plan_is_a_draft(self):
        """`is_active` defaults to True, so without this a plan created through any
        API is instantly sellable with unlimited everything and no priced tier."""
        plan = make_plan("draft-by-default")

        self.assertEqual(plan.status, Status.DRAFT)
        self.assertFalse(plan.is_active)

    def test_activating_makes_it_sellable(self):
        plan = make_plan("to-activate")

        plan.activate()

        plan.refresh_from_db()
        self.assertEqual(plan.status, Status.ACTIVE)
        self.assertTrue(plan.is_active)

    def test_retiring_makes_it_unsellable(self):
        plan = make_plan("to-retire", status=Status.ACTIVE)

        plan.deactivate()

        plan.refresh_from_db()
        self.assertEqual(plan.status, Status.RETIRED)
        self.assertFalse(plan.is_active)

    def test_a_retired_plan_can_come_back(self):
        """RETIRED is not terminal. A plan withdrawn last quarter can return."""
        plan = make_plan("returning", status=Status.RETIRED)

        plan.activate()

        plan.refresh_from_db()
        self.assertEqual(plan.status, Status.ACTIVE)
        self.assertTrue(plan.is_active)

    def test_a_draft_can_be_retired_without_ever_being_active(self):
        """There is no delete. An unwanted draft is put away the same way as
        anything else, so the console needs one verb rather than three."""
        plan = make_plan("abandoned")

        plan.deactivate()

        plan.refresh_from_db()
        self.assertEqual(plan.status, Status.RETIRED)


class IsActiveDerivationTests(TestCase):
    """`is_active` is filtered in six production paths and cannot be removed, so it
    has to keep telling the truth however the status was set."""

    def test_setting_status_directly_updates_is_active(self):
        plan = make_plan("direct")
        plan.status = Status.ACTIVE
        plan.save()

        plan.refresh_from_db()
        self.assertTrue(plan.is_active)

    def test_a_partial_save_of_status_carries_is_active_with_it(self):
        """`update_fields=["status"]` would otherwise write the new status and leave
        the derived column behind — a row half-updated, and queries filtering on
        `is_active` reading the old answer."""
        plan = make_plan("partial")

        plan.status = Status.ACTIVE
        plan.save(update_fields=["status", "updated_at"])

        plan.refresh_from_db()
        self.assertTrue(plan.is_active)

    def test_is_active_cannot_be_set_against_the_status(self):
        """Status is authoritative. Writing `is_active` directly is not a second way
        to control sellability — it is overwritten, which is what stops the two
        fields disagreeing."""
        plan = make_plan("contradicted")

        plan.is_active = True
        plan.save()

        plan.refresh_from_db()
        self.assertEqual(plan.status, Status.DRAFT)
        self.assertFalse(plan.is_active)

    def test_the_inherited_deactivate_is_not_silently_a_no_op(self):
        """**The trap this override exists for.** `BaseModelCore.deactivate()` writes
        `is_active=False` with `update_fields=["is_active"]`. With `is_active` derived
        from an unchanged status, that write would be recomputed straight back to
        True — a deactivate that appears to work and keeps selling the plan.
        """
        plan = make_plan("still-selling", status=Status.ACTIVE)
        self.assertTrue(plan.is_active)

        plan.deactivate()

        plan.refresh_from_db()
        self.assertFalse(plan.is_active)
        self.assertEqual(plan.status, Status.RETIRED)

    def test_the_six_existing_query_paths_still_see_the_right_plans(self):
        """Signup, eligible-plans, preview-change, change-plan, update-plan and the
        readiness audit all filter `is_active=True`. None of them know about status."""
        live = make_plan("live", status=Status.ACTIVE)
        make_plan("a-draft")
        make_plan("withdrawn", status=Status.RETIRED)

        found = list(Plan.objects.filter(is_active=True, is_deleted=False))

        self.assertEqual(found, [live])
