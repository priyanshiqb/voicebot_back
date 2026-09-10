"""
`MappedOrderingFilter` replaced `filters.OrderingFilter` on `BaseViewSet`, so it sits
in front of every list endpoint in the project — not just the three whose sorting was
reported broken.

That swap is only safe if a viewset declaring neither `ordering_field_map` nor
`ordering_tiebreaker` gets byte-identical ordering to the stock filter. These tests
compare the two directly on a view with neither attribute, and fail if the two ever
diverge on an untouched viewset.

No database: `get_ordering` only reads the request and the view.
"""

from django.test import SimpleTestCase
from rest_framework import filters
from rest_framework.request import Request
from rest_framework.test import APIRequestFactory

from core.models.contact import Contact
from utils.views.ordering import MappedOrderingFilter


class LegacyView:
    """A viewset as they all looked before the swap: neither new attribute."""

    ordering_fields = ["created_at", "updated_at", "first_name", "company"]
    ordering = ["-created_at"]


class MappedView(LegacyView):
    """One that opts in, to show the difference is confined to opting in."""

    ordering_fields = LegacyView.ordering_fields + ["full_name"]
    ordering_field_map = {"full_name": "_full_name"}
    ordering_tiebreaker = ("-created_at", "id")


class OrderingFilterCompatibilityTests(SimpleTestCase):
    QUERIES = [
        None,                          # no param at all -> the view default
        "",                            # present but empty
        "first_name",
        "-first_name",
        "company,-first_name",         # several keys
        "  first_name , -company ",    # whitespace around keys
        "not_a_column",                # wholly invalid -> falls back to the default
        "first_name,not_a_column",     # partly invalid -> invalid half dropped
        "created_at",
    ]

    def setUp(self):
        self.factory = APIRequestFactory()
        self.queryset = Contact.objects.all()
        self.stock = filters.OrderingFilter()
        self.mapped = MappedOrderingFilter()

    def _request(self, query):
        params = {} if query is None else {"ordering": query}
        return Request(self.factory.get("/", params))

    def _ordering(self, backend, view, query):
        return backend.get_ordering(self._request(query), self.queryset, view)

    def test_a_view_with_neither_attribute_is_unaffected_by_the_swap(self):
        for query in self.QUERIES:
            with self.subTest(ordering=query):
                stock = self._ordering(self.stock, LegacyView, query)
                mapped = self._ordering(self.mapped, LegacyView, query)
                # Compared as lists: DRF returns a tuple for the view default and a
                # list for a parsed param, and `order_by(*terms)` cannot tell them
                # apart. What must match is the terms and their order.
                self.assertEqual(list(stock or []), list(mapped or []))
                self.assertEqual(bool(stock), bool(mapped))

    def test_an_empty_ordering_stays_falsy_rather_than_becoming_an_empty_list(self):
        """
        `filter_queryset` tests `if ordering:` before calling `order_by`. Returning
        `[]` where the stock filter returns None would be harmless here but is exactly
        the kind of drift that bites a subclass overriding one and not the other.
        """

        class NoDefaultView:
            ordering_fields = ["created_at"]
            ordering = None

        self.assertIsNone(self._ordering(self.stock, NoDefaultView, "nope"))
        self.assertIsNone(self._ordering(self.mapped, NoDefaultView, "nope"))

    def test_validation_is_still_the_stock_filter_s(self):
        """A mapped name has to be in `ordering_fields` like any other."""

        class UnlistedMapView(LegacyView):
            # Mapped, but deliberately absent from ordering_fields.
            ordering_field_map = {"full_name": "_full_name"}

        self.assertEqual(
            list(self._ordering(self.mapped, UnlistedMapView, "full_name")),
            ["-created_at"],
        )

    def test_opting_in_is_what_changes_the_output(self):
        self.assertEqual(
            list(self._ordering(self.mapped, MappedView, "full_name")),
            ["_full_name", "-created_at", "id"],
        )

    def test_the_tiebreaker_never_duplicates_a_column_already_sorted_on(self):
        self.assertEqual(
            list(self._ordering(self.mapped, MappedView, "created_at")),
            ["created_at", "id"],
        )
