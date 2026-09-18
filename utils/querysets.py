"""
Batched queryset iteration that does not rely on server-side cursors.

`QuerySet.iterator(chunk_size=...)` opens a named Postgres cursor, which fails with
`InvalidCursorName` behind a pooler running in transaction mode. Keyset pagination
gives the same bounded-memory behaviour and works everywhere: each page is an ordinary
query that starts strictly after the last row of the previous one.
"""

from django.db.models import Q


def _after(fields, values):
    """Rows strictly after `values` in ascending `fields` order: a keyset condition.

    For ("created_at", "id") that is `created_at > v0 OR (created_at = v0 AND id > v1)`.
    """
    condition = Q()
    for index, field in enumerate(fields):
        step = Q(**{f"{field}__gt": values[index]})
        for earlier, value in zip(fields[:index], values[:index]):
            step &= Q(**{earlier: value})
        condition |= step
    return condition


def batched_queryset(queryset, batch_size=500, order_by=("pk",)):
    """
    Yield model instances in `order_by` order, `batch_size` at a time.

    `order_by` defaults to the primary key. Any other ordering must be ascending, name
    fields on the model itself, hold no nulls, and end in a unique field, so that no two
    rows share a position; otherwise rows at a page boundary are skipped or repeated.
    Pass an unsliced queryset: a slice cannot be reordered or filtered.
    """
    fields = tuple(order_by)
    if not fields or any(field.startswith("-") for field in fields):
        raise ValueError("batched_queryset pages on an ascending ordering only.")

    queryset = queryset.order_by(*fields)
    last = None

    while True:
        page = queryset if last is None else queryset.filter(_after(fields, last))
        rows = list(page[:batch_size])

        yield from rows
        # A short page is the last one; stopping here saves a final empty query.
        if len(rows) < batch_size:
            return
        last = tuple(getattr(rows[-1], field) for field in fields)


def batched_values(queryset, fields, batch_size=500):
    """
    Yield tuples of `fields` in primary-key order, `batch_size` at a time.

    `pk` is always fetched (as the last element) so the cursor can advance.
    """
    queryset = queryset.order_by("pk")
    selected = list(fields) + ["pk"]
    last_pk = None

    while True:
        page = queryset
        if last_pk is not None:
            page = page.filter(pk__gt=last_pk)

        rows = list(page.values_list(*selected)[:batch_size])

        yield from rows
        if len(rows) < batch_size:
            return
        last_pk = rows[-1][-1]
