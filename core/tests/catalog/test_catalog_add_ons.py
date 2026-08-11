"""
Add-ons.

An add-on is neither a variant nor a bundle, and modelling it as either is wrong in a
way that shows up in a real order:

* as a **variant** it becomes mutually exclusive with the thing it modifies — you
  could have Large *or* extra cheese, never both;
* as a **bundle** it becomes mandatory.

So it is a self-referential M2M with a kind check. The rules are enforced by an
`m2m_changed` receiver rather than `clean()`, because `clean()` cannot see an M2M at
all: the rows are written after the instance is saved, so `full_clean()` on a new item
validates an empty relation and passes. These tests go through
`item.add_on_items.add(...)` for that reason — the receiver is the only thing standing
between the shell and a cross-tenant link.

Every rejection below is wrapped in `transaction.atomic()`. Django runs `_add_items`
inside `atomic(savepoint=False)`, so an exception raised from a `pre_add` receiver
marks the *enclosing* transaction for rollback rather than unwinding a savepoint of
its own — and in a `TestCase`, the enclosing transaction is the test itself. Without
the wrapper the assertion passes and the next query raises
`TransactionManagementError`, which is the AGENTS.md §5 rule showing up in a place
that is not obviously a database exception.
"""

from django.core.exceptions import ValidationError
from django.db import transaction

from core.models.catalog_item import CatalogItem
from core.tests.catalog.base import CatalogTestCase


class AddOnTests(CatalogTestCase):
    def setUp(self):
        super().setUp()
        self.burger = self.make_item("Chicken Burger")
        self.cheese = self.make_item(
            "Extra Cheese", item_kind=CatalogItem.ItemKind.ADD_ON
        )

    def test_an_add_on_attaches_to_an_item(self):
        self.burger.add_on_items.add(self.cheese)

        self.assertEqual(list(self.burger.add_on_items.all()), [self.cheese])

    def test_the_relation_is_not_symmetrical(self):
        """Cheese goes on a burger, not the reverse. A symmetrical M2M would offer
        the burger as a topping for the cheese."""
        self.burger.add_on_items.add(self.cheese)

        self.assertEqual(self.cheese.add_on_items.count(), 0)
        self.assertEqual(list(self.cheese.add_on_for.all()), [self.burger])

    def test_a_non_add_on_cannot_be_attached_as_one(self):
        fries = self.make_item("Fries", item_kind=CatalogItem.ItemKind.PHYSICAL_GOOD)

        with transaction.atomic(), self.assertRaises(ValidationError) as caught:
            self.burger.add_on_items.add(fries)
        self.assertIn("ADD_ON", str(caught.exception))
        self.assertEqual(self.burger.add_on_items.count(), 0)

    def test_an_item_cannot_be_its_own_add_on(self):
        add_on = self.make_item("Sauce", item_kind=CatalogItem.ItemKind.ADD_ON)

        with transaction.atomic(), self.assertRaises(ValidationError):
            add_on.add_on_items.add(add_on)

        self.assertEqual(add_on.add_on_items.count(), 0)

    def test_a_cross_tenant_add_on_is_rejected(self):
        foreign_cheese = self.make_item(
            "Extra Cheese",
            tenant=self.other_tenant,
            item_kind=CatalogItem.ItemKind.ADD_ON,
        )

        with transaction.atomic(), self.assertRaises(ValidationError) as caught:
            self.burger.add_on_items.add(foreign_cheese)
        self.assertIn("tenant", str(caught.exception).lower())
        self.assertEqual(self.burger.add_on_items.count(), 0)

    def test_the_reverse_side_is_guarded_too(self):
        """`add_on.add_on_for.add(item)` reaches the same table by the other door.
        Guarding only the forward side leaves the rule half-true."""
        fries = self.make_item("Fries", item_kind=CatalogItem.ItemKind.PHYSICAL_GOOD)

        with transaction.atomic(), self.assertRaises(ValidationError):
            fries.add_on_for.add(self.burger)

        self.assertEqual(self.burger.add_on_items.count(), 0)

    def test_set_is_guarded_as_well_as_add(self):
        fries = self.make_item("Fries", item_kind=CatalogItem.ItemKind.PHYSICAL_GOOD)

        with transaction.atomic(), self.assertRaises(ValidationError):
            self.burger.add_on_items.set([self.cheese, fries])

        self.assertEqual(self.burger.add_on_items.count(), 0)

    def test_removing_an_add_on_is_never_blocked(self):
        """Validation on `pre_remove` would make a bad row unfixable."""
        self.burger.add_on_items.add(self.cheese)
        self.burger.add_on_items.remove(self.cheese)

        self.assertEqual(self.burger.add_on_items.count(), 0)

    def test_one_add_on_serves_many_items(self):
        wrap = self.make_item("Paneer Wrap")
        self.burger.add_on_items.add(self.cheese)
        wrap.add_on_items.add(self.cheese)

        self.assertEqual(self.cheese.add_on_for.count(), 2)

    def test_an_add_on_carries_its_own_price(self):
        """Add-ons are priced through variants like anything else, so "extra cheese"
        can itself be Regular or Double."""
        regular = self.make_variant(
            item=self.cheese, variant_label="Regular", price="40.00", is_default=True
        )
        self.make_variant(
            item=self.cheese, variant_label="Double", price="70.00", sort_order=1
        )
        self.burger.add_on_items.add(self.cheese)

        attached = self.burger.add_on_items.first()
        self.assertEqual(attached.variants.count(), 2)
        self.assertEqual(attached.default_variant.pk, regular.pk)
        self.assertEqual(regular.display_price, "₹40")
