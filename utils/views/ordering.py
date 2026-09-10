"""
Ordering helpers for list endpoints whose sortable columns are not plain model fields.

Several listings sort on something the client sees as one column but the database
stores as two (a contact's `full_name`), or reaches through a relation (an event
skill's user name). DRF's `OrderingFilter` can only order by names that exist on the
queryset, so those columns silently do nothing when passed as `?ordering=`.

`MappedOrderingFilter` closes that gap: the view keeps advertising the public name in
`ordering_fields`, and `ordering_field_map` says what that name resolves to on the
queryset — an annotation alias, a related path, or several of them in priority order.

It also optionally appends a tiebreaker, which matters more than it sounds. Sorting a
paginated list by a low-cardinality column — a status, a boolean, a company — leaves
most rows tied, and SQL gives no guarantee about the order of tied rows between two
queries. Page 2 can then repeat a row from page 1 and skip another entirely. Views
that opt in with `ordering_tiebreaker` get a total order, so paging is reproducible.
"""

from rest_framework import filters


def _expand(term, mapping):
    """Translate one ordering term ("name" / "-name") into real queryset terms."""
    descending = term.startswith("-")
    key = term[1:] if descending else term

    target = mapping.get(key, key)
    if isinstance(target, str):
        target = (target,)

    return [f"-{field}" if descending else field for field in target]


def _with_tiebreaker(terms, tiebreaker):
    """
    `terms` followed by any tiebreaker column it does not already sort on.

    Compared without the `-`, because a column already in the sort settles its own
    ties whichever direction it runs in, and repeating it would be noise in the SQL.
    """
    seen = {term.lstrip("-") for term in terms}
    return list(terms) + [
        term for term in tiebreaker if term.lstrip("-") not in seen
    ]


class MappedOrderingFilter(filters.OrderingFilter):
    """
    `OrderingFilter` that resolves public column names through `view.ordering_field_map`
    and appends `view.ordering_tiebreaker`.

    Validation is unchanged — a term still has to appear in `ordering_fields` to be
    honoured — so a view declaring neither attribute behaves exactly like the stock
    filter. Both are opt-in for that reason: this backend sits on every viewset in the
    project, and a sorting fix should not quietly rewrite the SQL of listings nobody
    reported a problem with.
    """

    def get_ordering(self, request, queryset, view):
        ordering = super().get_ordering(request, queryset, view)
        if not ordering:
            return ordering

        mapping = getattr(view, "ordering_field_map", None) or {}
        resolved = []
        for term in ordering:
            resolved.extend(_expand(term, mapping))

        tiebreaker = getattr(view, "ordering_tiebreaker", None)
        return _with_tiebreaker(resolved, tiebreaker) if tiebreaker else resolved


def resolve_ordering(
    request, allowed, default, field_map=None, tiebreaker=(), param="ordering"
):
    """
    Ordering terms for a hand-built queryset (an `@action` that never runs
    `filter_queryset`), so custom listings sort the same way the routed ones do.

    `allowed` is the set of public names a client may pass; anything else is dropped
    rather than raising, matching `OrderingFilter`'s behaviour. `default` applies when
    nothing usable was supplied — the caller always gets a non-empty result, because
    paginating an unordered queryset repeats and skips rows.
    """
    field_map = field_map or {}
    raw = request.query_params.get(param) or ""

    resolved = []
    for term in (part.strip() for part in raw.split(",")):
        if not term:
            continue
        if term.lstrip("-") not in allowed:
            continue
        resolved.extend(_expand(term, field_map))

    return _with_tiebreaker(resolved or list(default), tiebreaker)
