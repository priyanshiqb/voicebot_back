"""
Category API.

The two endpoints with behaviour of their own are `DELETE` and `bulk_delete`.

`DELETE` is **refused while the section is occupied**, and the refusal comes out of
the model as a Django `ValidationError` — which DRF does not catch. The viewset has to
translate it, or an entirely expected "you cannot delete a section with items in it"
escapes as a 500.

`bulk_delete` deliberately breaks from the generic implementation: the base loops
`soft_delete()` inside one transaction, so the first occupied category aborts every
deletion in the batch including the ones that would have succeeded.
"""

from django.db import connection
from django.test.utils import CaptureQueriesContext

from core.models.catalog_category import CatalogCategory
from core.tests.catalog.views.base import CATEGORIES, CatalogAPITestCase


class AuthTests(CatalogAPITestCase):
    def test_anonymous_is_refused(self):
        self.assertEqual(self.api_anon.get(CATEGORIES).status_code, 401)

    def test_a_machine_token_is_refused(self):
        """Closed by default: none of the catalog viewsets declares
        `vb_agent_allowed_actions`, and a viewset that quietly opened up would
        otherwise go unnoticed."""
        self.assertEqual(self.api_machine.get(CATEGORIES).status_code, 403)
        self.assertEqual(
            self.api_machine.post(CATEGORIES, {"name": "X"}, format="json").status_code,
            403,
        )


class CrudTests(CatalogAPITestCase):
    def test_create_returns_201_with_the_envelope(self):
        response = self.api.post(CATEGORIES, {"name": "Menu"}, format="json")
        data = self.assert_ok(response, 201)

        self.assertEqual(response.data["message"], "Created successfully")
        self.assertEqual(data["name"], "Menu")
        self.assertEqual(data["depth"], 0)
        self.assertEqual(data["item_count"], 0)

    def test_create_stamps_the_callers_tenant_and_audit_user(self):
        data = self.assert_ok(
            self.api.post(CATEGORIES, {"name": "Menu"}, format="json"), 201
        )
        category = CatalogCategory.objects.get(pk=data["id"])
        self.assertEqual(category.tenant_id, self.tenant.id)
        self.assertEqual(category.created_by, self.user)

    def test_a_child_derives_its_depth(self):
        root = self.assert_ok(
            self.api.post(CATEGORIES, {"name": "Menu"}, format="json"), 201
        )
        child = self.assert_ok(
            self.api.post(
                CATEGORIES, {"name": "Starters", "parent": root["id"]}, format="json"
            ),
            201,
        )
        self.assertEqual(child["depth"], 1)
        self.assertEqual(child["parent"]["name"], "Menu")

    def test_retrieve(self):
        category = self.make_category("Menu")
        data = self.assert_ok(self.api.get(f"{CATEGORIES}{category.pk}/"))
        self.assertEqual(data["name"], "Menu")

    def test_partial_update(self):
        category = self.make_category("Menu")
        data = self.assert_ok(
            self.api.patch(
                f"{CATEGORIES}{category.pk}/", {"sort_order": 5}, format="json"
            )
        )
        self.assertEqual(data["sort_order"], 5)
        self.assertEqual(data["name"], "Menu")

    def test_re_parenting_through_the_api_cascades_depth(self):
        root = self.make_category("Menu")
        child = self.make_category("Starters", parent=root)
        grandchild = self.make_category("Vegetarian", parent=child)

        self.assert_ok(
            self.api.patch(f"{CATEGORIES}{child.pk}/", {"parent": None}, format="json")
        )

        grandchild.refresh_from_db()
        self.assertEqual(grandchild.depth, 1)

    def test_a_duplicate_name_is_a_400_on_the_name_field(self):
        self.make_category("Menu")
        self.assert_error(
            self.api.post(CATEGORIES, {"name": "menu"}, format="json"),
            field="name",
            contains="at the top level",
        )

    def test_depth_is_not_writable(self):
        data = self.assert_ok(
            self.api.post(CATEGORIES, {"name": "Menu", "depth": 2}, format="json"), 201
        )
        self.assertEqual(data["depth"], 0)


class ListingTests(CatalogAPITestCase):
    def setUp(self):
        super().setUp()
        self.root = self.make_category("Menu", sort_order=1)
        self.child = self.make_category("Starters", parent=self.root)
        self.drinks = self.make_category("Drinks", sort_order=0, is_active=False)

    def test_the_list_is_paginated_with_meta(self):
        rows = self.assert_paginated(self.api.get(CATEGORIES), count=3)
        self.assertEqual(len(rows), 3)

    def test_the_default_order_is_tree_order(self):
        """Depth first, then sort_order — a category list is browsed, and "most
        recently added" puts a leaf above the root it belongs to."""
        rows = self.assert_paginated(self.api.get(CATEGORIES))
        self.assertEqual([r["name"] for r in rows], ["Drinks", "Menu", "Starters"])

    def test_the_list_row_flattens_the_parent(self):
        rows = self.assert_paginated(self.api.get(CATEGORIES))
        starters = next(r for r in rows if r["name"] == "Starters")
        self.assertEqual(starters["parent_name"], "Menu")
        self.assertNotIn("created_by", starters)

    def test_filter_by_parent(self):
        rows = self.assert_paginated(
            self.api.get(CATEGORIES, {"parent": str(self.root.pk)}), count=1
        )
        self.assertEqual(rows[0]["name"], "Starters")

    def test_filter_by_roots_only(self):
        rows = self.assert_paginated(
            self.api.get(CATEGORIES, {"parent__isnull": "true"}), count=2
        )
        self.assertEqual({r["name"] for r in rows}, {"Menu", "Drinks"})

    def test_filter_by_depth(self):
        self.assert_paginated(self.api.get(CATEGORIES, {"depth": 1}), count=1)

    def test_filter_by_is_active(self):
        self.assert_paginated(self.api.get(CATEGORIES, {"is_active": "false"}), count=1)

    def test_search_matches_name_and_description(self):
        self.make_category("Sides", description="Crisps and salads")
        self.assert_paginated(self.api.get(CATEGORIES, {"search": "Start"}), count=1)
        self.assert_paginated(self.api.get(CATEGORIES, {"search": "salads"}), count=1)

    def test_ordering_by_name_descending(self):
        rows = self.assert_paginated(self.api.get(CATEGORIES, {"ordering": "-name"}))
        self.assertEqual([r["name"] for r in rows], ["Starters", "Menu", "Drinks"])

    def test_item_count_is_annotated(self):
        self.make_item(category=self.child)
        rows = self.assert_paginated(self.api.get(CATEGORIES))
        starters = next(r for r in rows if r["name"] == "Starters")
        self.assertEqual(starters["item_count"], 1)

    def test_the_list_query_count_does_not_grow_with_rows(self):
        """The invariant the annotation exists for, asserted as an invariant rather
        than a magic number.

        This caught a real one: the serializer read the annotation with
        `getattr(...) or obj.items.count()`, and an annotation of **0** is falsy — so
        every empty category quietly fell through to a per-row query while still
        reporting the right number.
        """
        with CaptureQueriesContext(connection) as baseline:
            self.api.get(CATEGORIES)

        for index in range(5):
            self.make_category(f"Extra {index}")

        with CaptureQueriesContext(connection) as grown:
            self.api.get(CATEGORIES)

        self.assertEqual(
            len(grown.captured_queries),
            len(baseline.captured_queries),
            "listing more categories issued more queries — the annotation is not "
            "being used",
        )

    def test_item_counts_are_correct_for_empty_and_occupied_sections(self):
        """The other half of the bug above: whatever the query count, the numbers have
        to stay right."""
        self.make_item(category=self.child)

        rows = self.assert_paginated(self.api.get(CATEGORIES))
        by_name = {row["name"]: row["item_count"] for row in rows}
        self.assertEqual(by_name["Starters"], 1)
        self.assertEqual(by_name["Menu"], 0)
        self.assertEqual(by_name["Drinks"], 0)


class TreeTests(CatalogAPITestCase):
    def setUp(self):
        super().setUp()
        self.root = self.make_category("Menu")
        self.child = self.make_category("Starters", parent=self.root)
        self.grandchild = self.make_category("Vegetarian", parent=self.child)

    def test_the_tree_nests_three_levels_in_one_response(self):
        data = self.assert_ok(self.api.get(f"{CATEGORIES}tree/"))
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["name"], "Menu")
        self.assertEqual(data[0]["children"][0]["name"], "Starters")
        self.assertEqual(data[0]["children"][0]["children"][0]["name"], "Vegetarian")

    def test_the_tree_is_unpaginated(self):
        self.assertNotIn("meta", self.api.get(f"{CATEGORIES}tree/").data)

    def test_the_tree_starts_from_the_roots_only(self):
        self.make_category("Drinks")
        data = self.assert_ok(self.api.get(f"{CATEGORIES}tree/"))
        self.assertEqual({node["name"] for node in data}, {"Menu", "Drinks"})

    def test_the_tree_is_tenant_scoped(self):
        self.make_category("Theirs", tenant=self.other_tenant)
        data = self.assert_ok(self.api.get(f"{CATEGORIES}tree/"))
        self.assertNotIn("Theirs", [node["name"] for node in data])


class DeleteTests(CatalogAPITestCase):
    def test_delete_is_refused_while_items_remain(self):
        """A Django ValidationError out of the model, translated. Untranslated it is a
        500 for an entirely expected outcome."""
        category = self.make_category("Menu")
        self.make_item(category=category)

        response = self.api.delete(f"{CATEGORIES}{category.pk}/")
        self.assert_error(response, 400, contains="still contains 1 item")

        category.refresh_from_db()
        self.assertFalse(category.is_deleted)

    def test_delete_is_refused_while_sub_categories_remain(self):
        root = self.make_category("Menu")
        self.make_category("Starters", parent=root)

        self.assert_error(
            self.api.delete(f"{CATEGORIES}{root.pk}/"), 400, contains="sub-categor"
        )

    def test_delete_succeeds_once_empty(self):
        category = self.make_category("Menu")
        response = self.api.delete(f"{CATEGORIES}{category.pk}/")

        self.assertEqual(response.status_code, 200, response.data)
        category.refresh_from_db()
        self.assertTrue(category.is_deleted)

    def test_a_deleted_category_disappears_from_the_list(self):
        category = self.make_category("Menu")
        self.api.delete(f"{CATEGORIES}{category.pk}/")
        self.assert_paginated(self.api.get(CATEGORIES), count=0)

    def test_bulk_delete_reports_per_id_rather_than_aborting(self):
        """The whole reason this action is overridden. The generic version would roll
        the empty category's deletion back along with the occupied one's."""
        empty = self.make_category("Empty")
        occupied = self.make_category("Occupied")
        self.make_item(category=occupied)

        data = self.assert_ok(
            self.api.post(
                f"{CATEGORIES}bulk_delete/",
                {"ids": [str(empty.pk), str(occupied.pk)]},
                format="json",
            )
        )

        self.assertEqual(data["deleted"], [str(empty.pk)])
        self.assertIn(str(occupied.pk), data["refused"])
        self.assertIn("still contains", data["refused"][str(occupied.pk)])

        empty.refresh_from_db()
        occupied.refresh_from_db()
        self.assertTrue(empty.is_deleted)
        self.assertFalse(occupied.is_deleted)

    def test_bulk_delete_rejects_an_empty_list(self):
        self.assert_error(
            self.api.post(f"{CATEGORIES}bulk_delete/", {"ids": []}, format="json"),
            400,
            contains="non-empty list",
        )

    def test_bulk_delete_rejects_a_foreign_id_without_deleting_anything(self):
        mine = self.make_category("Mine")
        theirs = self.make_category("Theirs", tenant=self.other_tenant)

        self.assert_error(
            self.api.post(
                f"{CATEGORIES}bulk_delete/",
                {"ids": [str(mine.pk), str(theirs.pk)]},
                format="json",
            ),
            400,
            field="ids",
        )

        mine.refresh_from_db()
        self.assertFalse(mine.is_deleted)


class DropdownTests(CatalogAPITestCase):
    def test_the_dropdown_indents_by_depth(self):
        root = self.make_category("Menu")
        self.make_category("Starters", parent=root)

        rows = self.assert_paginated(self.api.get(f"{CATEGORIES}dropdown/"))
        labels = [row["label"] for row in rows]
        self.assertIn("Menu", labels)
        self.assertIn("— Starters", labels)
        self.assertEqual(set(rows[0]), {"label", "value"})

    def test_the_dropdown_limit_is_capped(self):
        self.assert_error(
            self.api.get(f"{CATEGORIES}dropdown/", {"limit": 5000}),
            400,
            contains="cannot exceed",
        )


class TenantIsolationTests(CatalogAPITestCase):
    def setUp(self):
        super().setUp()
        self.mine = self.make_category("Menu")
        self.theirs = self.make_category("Their Menu", tenant=self.other_tenant)

    def test_the_list_shows_only_the_callers_tenant(self):
        rows = self.assert_paginated(self.api.get(CATEGORIES), count=1)
        self.assertEqual(rows[0]["name"], "Menu")

    def test_another_tenants_category_is_404_not_403(self):
        """403 would confirm the row exists, which is the leak this avoids."""
        self.assertEqual(
            self.api.get(f"{CATEGORIES}{self.theirs.pk}/").status_code, 404
        )

    def test_another_tenants_category_cannot_be_updated(self):
        self.assertEqual(
            self.api.patch(
                f"{CATEGORIES}{self.theirs.pk}/", {"name": "Hijacked"}, format="json"
            ).status_code,
            404,
        )

    def test_another_tenants_category_cannot_be_deleted(self):
        self.assertEqual(
            self.api.delete(f"{CATEGORIES}{self.theirs.pk}/").status_code, 404
        )

    def test_a_cross_tenant_parent_is_a_400_on_parent(self):
        self.assert_error(
            self.api.post(
                CATEGORIES,
                {"name": "Starters", "parent": str(self.theirs.pk)},
                format="json",
            ),
            field="parent",
            contains="different tenant",
        )
