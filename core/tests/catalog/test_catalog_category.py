"""
The catalog category tree.

Two things here can be wrong without looking wrong:

1. **The NULL-parent uniqueness trap.** Postgres treats NULLs as distinct inside a
   unique index, so a single constraint over (tenant, lower(name), parent) silently
   permits two root categories with the same name. The model carries two constraints
   for this reason, and `test_two_root_categories_cannot_share_a_name` is the test
   that would catch the single-constraint version.

2. **`depth` drifting from the tree.** It is a stored derived column, so a re-parent
   has to cascade. Without the cascade a grandchild keeps a stale depth and the check
   constraint that bounds the tree stops meaning anything.
"""

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from core.models.catalog_category import (
    MAX_CATEGORY_DEPTH,
    MAX_DEPTH_INDEX,
    CatalogCategory,
)
from core.tests.catalog.base import AUDIT_FIELDS, CatalogTestCase


class UniquenessTests(CatalogTestCase):
    def test_two_root_categories_cannot_share_a_name(self):
        """The NULL-parent trap. A single UniqueConstraint including `parent` would
        let this through, because NULL != NULL in a Postgres unique index."""
        self.make_category("Services")
        with transaction.atomic(), self.assertRaises(IntegrityError):
            self.make_category("Services")

    def test_root_names_collide_case_insensitively(self):
        self.make_category("Services")
        with transaction.atomic(), self.assertRaises(IntegrityError):
            self.make_category("SERVICES")

    def test_the_same_name_is_allowed_under_two_different_parents(self):
        """"Red" under Wine and "Red" under Paint are different things. A constraint
        that ignored `parent` would refuse the second one."""
        wine = self.make_category("Wine")
        paint = self.make_category("Paint")

        self.make_category("Red", parent=wine)
        self.make_category("Red", parent=paint)

        self.assertEqual(CatalogCategory.objects.filter(name="Red").count(), 2)

    def test_siblings_cannot_share_a_name(self):
        wine = self.make_category("Wine")
        self.make_category("Red", parent=wine)
        with transaction.atomic(), self.assertRaises(IntegrityError):
            self.make_category("red", parent=wine)

    def test_the_same_name_is_allowed_in_two_tenants(self):
        self.make_category("Services")
        self.make_category("Services", tenant=self.other_tenant)
        self.assertEqual(CatalogCategory.objects.filter(name="Services").count(), 2)

    def test_soft_deleting_frees_the_name(self):
        original = self.make_category("Seasonal")
        original.soft_delete()
        replacement = self.make_category("Seasonal")
        self.assertNotEqual(original.pk, replacement.pk)


class DepthTests(CatalogTestCase):
    def test_depth_is_derived_at_every_level(self):
        root = self.make_category("Menu")
        child = self.make_category("Starters", parent=root)
        grandchild = self.make_category("Vegetarian", parent=child)

        self.assertEqual((root.depth, child.depth, grandchild.depth), (0, 1, 2))

    def test_a_fourth_level_is_rejected(self):
        node = self.make_category("L0")
        for index in range(1, MAX_CATEGORY_DEPTH):
            node = self.make_category(f"L{index}", parent=node)

        too_deep = CatalogCategory(
            tenant=self.tenant, name="L3", parent=node
        )
        with self.assertRaises(ValidationError) as caught:
            too_deep.full_clean(exclude=AUDIT_FIELDS)
        self.assertIn("parent", caught.exception.message_dict)

    def test_the_depth_bound_is_enforced_by_the_database_too(self):
        """`clean()` is not what holds this line — a management command or data fix
        writing straight through the ORM must not be able to flatten the bound."""
        category = self.make_category("Menu")
        with transaction.atomic(), self.assertRaises(IntegrityError):
            CatalogCategory.objects.filter(pk=category.pk).update(
                depth=MAX_DEPTH_INDEX + 1
            )

    def test_re_parenting_cascades_depth_to_descendants(self):
        root = self.make_category("Menu")
        child = self.make_category("Starters", parent=root)
        grandchild = self.make_category("Vegetarian", parent=child)

        # Promote the middle node to a root. Its subtree has to follow it up.
        child.parent = None
        child.save()

        child.refresh_from_db()
        grandchild.refresh_from_db()
        self.assertEqual(child.depth, 0)
        self.assertEqual(grandchild.depth, 1)

    def test_re_parenting_that_would_overflow_the_subtree_is_rejected(self):
        """Validating only the moved node's own depth is not enough: this move puts
        the node at a legal depth and its grandchild at an illegal one."""
        root = self.make_category("Menu")
        child = self.make_category("Starters", parent=root)
        self.make_category("Vegetarian", parent=child)

        other_root = self.make_category("Drinks")
        other_child = self.make_category("Hot", parent=other_root)

        child.parent = other_child
        with self.assertRaises(ValidationError) as caught:
            child.full_clean(exclude=AUDIT_FIELDS)
        self.assertIn("parent", caught.exception.message_dict)

    def test_housekeeping_saves_do_not_touch_the_tree(self):
        """`deactivate` names its update_fields, so it must not re-derive depth or
        walk the subtree. A row must always be deactivatable."""
        root = self.make_category("Menu")
        child = self.make_category("Starters", parent=root)

        child.deactivate()

        child.refresh_from_db()
        self.assertFalse(child.is_active)
        self.assertEqual(child.depth, 1)


class CycleTests(CatalogTestCase):
    def test_a_category_cannot_be_its_own_parent(self):
        category = self.make_category("Menu")
        category.parent = category
        with self.assertRaises(ValidationError):
            category.full_clean(exclude=AUDIT_FIELDS)

    def test_a_category_cannot_be_moved_under_its_own_descendant(self):
        root = self.make_category("Menu")
        child = self.make_category("Starters", parent=root)

        root.parent = child
        with self.assertRaises(ValidationError) as caught:
            root.full_clean(exclude=AUDIT_FIELDS)
        self.assertIn("loop", str(caught.exception).lower())

    def test_a_cross_tenant_parent_is_rejected(self):
        """Otherwise a tenant grafts its category under a stranger's tree by naming
        an id — the same shape of hole TenantSettings.clean() closes."""
        foreign = self.make_category("Menu", tenant=self.other_tenant)
        category = CatalogCategory(tenant=self.tenant, name="Starters", parent=foreign)

        with self.assertRaises(ValidationError) as caught:
            category.full_clean(exclude=AUDIT_FIELDS)
        self.assertIn("parent", caught.exception.message_dict)


class DeletionTests(CatalogTestCase):
    def test_soft_delete_refuses_while_sub_categories_remain(self):
        root = self.make_category("Menu")
        self.make_category("Starters", parent=root)

        with self.assertRaises(ValidationError) as caught:
            root.soft_delete()
        self.assertIn("sub-categor", str(caught.exception))

        root.refresh_from_db()
        self.assertFalse(root.is_deleted)

    def test_soft_delete_refuses_while_items_remain(self):
        category = self.make_category("Menu")
        self.make_item(category=category)

        with self.assertRaises(ValidationError) as caught:
            category.soft_delete()
        self.assertIn("item", str(caught.exception))

        category.refresh_from_db()
        self.assertFalse(category.is_deleted)

    def test_soft_delete_succeeds_once_the_section_is_empty(self):
        root = self.make_category("Menu")
        child = self.make_category("Starters", parent=root)

        child.soft_delete()
        root.soft_delete()

        root.refresh_from_db()
        self.assertTrue(root.is_deleted)
        self.assertIsNotNone(root.deleted_at)

    def test_a_soft_deleted_child_does_not_block_its_parent(self):
        """`children` uses the default manager, so a deleted child is not a child."""
        root = self.make_category("Menu")
        child = self.make_category("Starters", parent=root)
        child.soft_delete()

        root.soft_delete()
        self.assertTrue(CatalogCategory.all_objects.get(pk=root.pk).is_deleted)

    def test_restore_into_a_taken_name_raises_validation_error(self):
        """Not IntegrityError. The partial constraint means the name may have been
        reused while this row sat deleted, and a raw database error out of the admin
        is not an answer anyone can act on."""
        original = self.make_category("Seasonal")
        original.soft_delete()
        self.make_category("seasonal")

        deleted = CatalogCategory.all_objects.get(pk=original.pk)
        with self.assertRaises(ValidationError) as caught:
            deleted.restore()
        self.assertIn("name", caught.exception.message_dict)

    def test_restore_succeeds_when_the_name_is_still_free(self):
        category = self.make_category("Seasonal")
        category.soft_delete()

        deleted = CatalogCategory.all_objects.get(pk=category.pk)
        deleted.restore()

        self.assertTrue(CatalogCategory.objects.filter(pk=category.pk).exists())
