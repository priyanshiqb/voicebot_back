"""
Category serializers.

The rules worth the most attention here are the ones DRF would not enforce on its own:

* **`depth` must not be writable.** It is derived from `parent`, and a client-supplied
  value would be rejected by the check constraint as an `IntegrityError` — a 500 — not
  a field error.
* **The model's `clean()` has to be re-run.** DRF never calls `full_clean()`, so cycle
  detection, the depth cap and the subtree-height rule exist only in the admin and the
  shell unless the serializer invokes them.
* **A failed validation must not mutate the live instance.** `validate()` needs to
  probe "what would this look like after the change", and doing that on
  `self.instance` half-applies the edit when validation then fails.
"""

from core.models.catalog_category import MAX_CATEGORY_DEPTH, CatalogCategory
from core.serializers.catalog_category import (
    CatalogCategoryDropdownSerializer,
    CatalogCategoryListSerializer,
    CatalogCategoryRefSerializer,
    CatalogCategorySerializer,
    CatalogCategoryTreeSerializer,
)
from core.tests.catalog.serializers.base import CatalogSerializerTestCase


class NameValidationTests(CatalogSerializerTestCase):
    def test_a_name_is_stripped(self):
        serializer = self.build(CatalogCategorySerializer, {"name": "  Menu  "})
        category = self.save_valid(serializer)
        self.assertEqual(category.name, "Menu")

    def test_a_one_character_name_is_rejected(self):
        serializer = self.build(CatalogCategorySerializer, {"name": "M"})
        self.assert_field_error(serializer, "name", "at least 2 characters")

    def test_a_whitespace_only_name_is_rejected(self):
        """Stripped first, so "   " is a one-character name rather than a valid one."""
        serializer = self.build(CatalogCategorySerializer, {"name": "     "})
        self.assert_field_error(serializer, "name")

    def test_an_over_long_name_is_rejected(self):
        serializer = self.build(CatalogCategorySerializer, {"name": "x" * 151})
        self.assert_field_error(serializer, "name")

    def test_a_missing_name_is_rejected(self):
        serializer = self.build(CatalogCategorySerializer, {})
        self.assert_field_error(serializer, "name")


class DerivedFieldTests(CatalogSerializerTestCase):
    def test_depth_is_ignored_when_supplied(self):
        """Writable, a client could claim a depth the tree does not support and the
        check constraint would answer with an IntegrityError instead of a 400."""
        serializer = self.build(
            CatalogCategorySerializer, {"name": "Menu", "depth": 5}
        )
        category = self.save_valid(serializer)
        self.assertEqual(category.depth, 0)

    def test_depth_is_derived_from_the_parent(self):
        root = self.make_category("Menu")
        serializer = self.build(
            CatalogCategorySerializer, {"name": "Starters", "parent": str(root.pk)}
        )
        child = self.save_valid(serializer)
        self.assertEqual(child.depth, 1)

    def test_tenant_is_not_writable(self):
        serializer = self.build(
            CatalogCategorySerializer,
            {"name": "Menu", "tenant": str(self.other_tenant.pk)},
        )
        category = self.save_valid(serializer)
        self.assertEqual(category.tenant_id, self.tenant.id)

    def test_the_counts_are_read_only_and_computed(self):
        category = self.make_category("Menu")
        self.make_item(category=category)
        self.make_category("Starters", parent=category)

        data = CatalogCategorySerializer(category, context=self.context()).data
        self.assertEqual(data["item_count"], 1)
        self.assertEqual(data["child_count"], 1)

    def test_the_counts_prefer_a_queryset_annotation(self):
        """The list view annotates both to avoid two extra queries per row. If the
        serializer stopped reading the annotation the N+1 would come back silently."""
        category = self.make_category("Menu")
        category.annotated_item_count = 99
        data = CatalogCategorySerializer(category, context=self.context()).data
        self.assertEqual(data["item_count"], 99)


class ParentValidationTests(CatalogSerializerTestCase):
    def test_a_cross_tenant_parent_is_rejected_on_the_parent_field(self):
        foreign = self.make_category("Menu", tenant=self.other_tenant)
        serializer = self.build(
            CatalogCategorySerializer, {"name": "Starters", "parent": str(foreign.pk)}
        )
        self.assert_field_error(serializer, "parent", "different tenant")

    def test_a_fourth_level_is_rejected(self):
        node = self.make_category("L0")
        for index in range(1, MAX_CATEGORY_DEPTH):
            node = self.make_category(f"L{index}", parent=node)

        serializer = self.build(
            CatalogCategorySerializer, {"name": "L3", "parent": str(node.pk)}
        )
        self.assert_field_error(serializer, "parent", "3 levels deep")

    def test_moving_a_category_under_its_own_child_is_rejected(self):
        root = self.make_category("Menu")
        child = self.make_category("Starters", parent=root)

        serializer = self.build(
            CatalogCategorySerializer,
            {"parent": str(child.pk)},
            instance=root,
            partial=True,
        )
        self.assert_field_error(serializer, "parent", "loop")

    def test_a_move_that_would_push_descendants_too_deep_is_rejected(self):
        """Validating only the moved node is not enough: this move puts it at a legal
        depth and its grandchild at an illegal one."""
        root = self.make_category("Menu")
        child = self.make_category("Starters", parent=root)
        self.make_category("Vegetarian", parent=child)

        other_root = self.make_category("Drinks")
        other_child = self.make_category("Hot", parent=other_root)

        serializer = self.build(
            CatalogCategorySerializer,
            {"parent": str(other_child.pk)},
            instance=child,
            partial=True,
        )
        self.assert_field_error(serializer, "parent", "sub-categories of its own")

    def test_a_failed_validation_does_not_mutate_the_instance(self):
        """The `copy.copy` guard. `validate()` has to probe the post-change shape, and
        doing that on `self.instance` leaves the rejected parent applied in memory —
        which a caller that goes on to save for another reason would then persist."""
        root = self.make_category("Menu")
        child = self.make_category("Starters", parent=root)

        serializer = self.build(
            CatalogCategorySerializer,
            {"parent": str(child.pk)},
            instance=root,
            partial=True,
        )
        self.assertFalse(serializer.is_valid())
        self.assertIsNone(serializer.instance.parent_id)
        self.assertEqual(serializer.instance.name, "Menu")

    def test_clearing_the_parent_promotes_to_a_root(self):
        root = self.make_category("Menu")
        child = self.make_category("Starters", parent=root)

        serializer = self.build(
            CatalogCategorySerializer, {"parent": None}, instance=child, partial=True
        )
        self.assertTrue(serializer.is_valid(), dict(serializer.errors))
        updated = serializer.save(updated_by=self.user)
        self.assertIsNone(updated.parent_id)
        self.assertEqual(updated.depth, 0)


class UniquenessTests(CatalogSerializerTestCase):
    def test_a_duplicate_root_name_is_a_field_error_not_a_500(self):
        self.make_category("Services")
        serializer = self.build(CatalogCategorySerializer, {"name": "Services"})
        self.assert_field_error(serializer, "name", "at the top level")

    def test_root_names_collide_case_insensitively(self):
        """Matched with `Lower()` so the check agrees with the constraint. A raw
        comparison would pass "SERVICES" here and then fail in the database."""
        self.make_category("Services")
        serializer = self.build(CatalogCategorySerializer, {"name": "SERVICES"})
        self.assert_field_error(serializer, "name")

    def test_a_duplicate_sibling_name_names_its_parent(self):
        wine = self.make_category("Wine")
        self.make_category("Red", parent=wine)

        serializer = self.build(
            CatalogCategorySerializer, {"name": "red", "parent": str(wine.pk)}
        )
        self.assert_field_error(serializer, "name", "under 'Wine'")

    def test_the_same_name_under_two_parents_is_allowed(self):
        wine = self.make_category("Wine")
        paint = self.make_category("Paint")
        self.make_category("Red", parent=wine)

        serializer = self.build(
            CatalogCategorySerializer, {"name": "Red", "parent": str(paint.pk)}
        )
        self.assertTrue(serializer.is_valid(), dict(serializer.errors))

    def test_the_same_name_in_another_tenant_is_allowed(self):
        self.make_category("Services", tenant=self.other_tenant)
        serializer = self.build(CatalogCategorySerializer, {"name": "Services"})
        self.assertTrue(serializer.is_valid(), dict(serializer.errors))

    def test_renaming_a_category_to_its_own_name_is_allowed(self):
        """The clash query has to exclude the instance being edited, or every PATCH
        that touches any other field fails on the name."""
        category = self.make_category("Services")
        serializer = self.build(
            CatalogCategorySerializer,
            {"name": "Services", "sort_order": 3},
            instance=category,
            partial=True,
        )
        self.assertTrue(serializer.is_valid(), dict(serializer.errors))

    def test_a_soft_deleted_sibling_does_not_block_the_name(self):
        original = self.make_category("Seasonal")
        original.soft_delete()

        serializer = self.build(CatalogCategorySerializer, {"name": "Seasonal"})
        self.assertTrue(serializer.is_valid(), dict(serializer.errors))


class RepresentationTests(CatalogSerializerTestCase):
    def setUp(self):
        super().setUp()
        self.root = self.make_category("Menu", sort_order=1)
        self.child = self.make_category("Starters", parent=self.root)
        self.grandchild = self.make_category("Vegetarian", parent=self.child)

    def test_the_ref_serializer_is_minimal(self):
        data = CatalogCategoryRefSerializer(self.child).data
        self.assertEqual(set(data), {"id", "name", "depth"})

    def test_the_main_serializer_nests_the_parent(self):
        data = CatalogCategorySerializer(self.child, context=self.context()).data
        self.assertEqual(data["parent"]["name"], "Menu")
        self.assertEqual(data["parent"]["depth"], 0)

    def test_the_list_serializer_flattens_the_parent_and_drops_the_audit_block(self):
        """A category list renders on every catalog screen; the audit block is three
        user joins per row that nothing displays."""
        data = CatalogCategoryListSerializer(self.child).data
        self.assertEqual(data["parent_name"], "Menu")
        for absent in ("created_by", "updated_by", "deleted_by", "tenant"):
            self.assertNotIn(absent, data)

    def test_a_root_has_a_null_parent_name(self):
        self.assertIsNone(CatalogCategoryListSerializer(self.root).data["parent_name"])

    def test_the_dropdown_label_carries_the_nesting(self):
        """A flat picker of section names is ambiguous the moment two parents both
        have a "Red"."""
        self.assertEqual(
            CatalogCategoryDropdownSerializer(self.root).data["label"], "Menu"
        )
        self.assertEqual(
            CatalogCategoryDropdownSerializer(self.child).data["label"], "— Starters"
        )
        self.assertEqual(
            CatalogCategoryDropdownSerializer(self.grandchild).data["label"],
            "— — Vegetarian",
        )

    def test_the_dropdown_value_is_the_id(self):
        data = CatalogCategoryDropdownSerializer(self.root).data
        self.assertEqual(set(data), {"label", "value"})
        self.assertEqual(str(data["value"]), str(self.root.pk))

    def test_the_tree_serializer_nests_children(self):
        data = CatalogCategoryTreeSerializer(
            self.root, context=self.context()
        ).data
        self.assertEqual(data["name"], "Menu")
        self.assertEqual(data["children"][0]["name"], "Starters")
        self.assertEqual(data["children"][0]["children"][0]["name"], "Vegetarian")

    def test_the_tree_stops_recursing_at_the_depth_cap(self):
        """What makes an unbounded-looking recursion safe. The leaf reports `[]`
        without issuing a query for children that cannot exist."""
        data = CatalogCategoryTreeSerializer(
            self.grandchild, context=self.context()
        ).data
        self.assertEqual(data["children"], [])

    def test_the_tree_orders_children_by_sort_order(self):
        self.make_category("Soups", parent=self.root, sort_order=0)
        data = CatalogCategoryTreeSerializer(self.root, context=self.context()).data
        self.assertEqual([c["name"] for c in data["children"]], ["Soups", "Starters"])

    def test_the_tree_excludes_soft_deleted_children(self):
        self.grandchild.soft_delete()
        self.child.refresh_from_db()
        data = CatalogCategoryTreeSerializer(self.child, context=self.context()).data
        self.assertEqual(data["children"], [])


class CreateTests(CatalogSerializerTestCase):
    def test_a_minimal_create_lands_a_root_category(self):
        serializer = self.build(CatalogCategorySerializer, {"name": "Menu"})
        category = self.save_valid(serializer)

        self.assertEqual(category.depth, 0)
        self.assertIsNone(category.parent_id)
        self.assertEqual(category.sort_order, 0)
        self.assertTrue(category.is_active)
        self.assertEqual(category.description, "")
        self.assertEqual(category.created_by, self.user)

    def test_a_full_create_round_trips(self):
        root = self.make_category("Menu")
        serializer = self.build(
            CatalogCategorySerializer,
            {
                "name": "Starters",
                "parent": str(root.pk),
                "description": "Small plates",
                "sort_order": 2,
                "is_active": False,
            },
        )
        category = self.save_valid(serializer)

        self.assertEqual(category.parent_id, root.pk)
        self.assertEqual(category.description, "Small plates")
        self.assertEqual(category.sort_order, 2)
        self.assertFalse(category.is_active)

    def test_the_saved_row_is_in_the_callers_tenant(self):
        serializer = self.build(CatalogCategorySerializer, {"name": "Menu"})
        category = self.save_valid(serializer)
        self.assertEqual(category.tenant_id, self.tenant.id)
        self.assertTrue(
            CatalogCategory.objects.filter(
                pk=category.pk, tenant_id=self.tenant.id
            ).exists()
        )
