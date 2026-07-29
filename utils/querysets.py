"""
Batched queryset iteration that does not rely on server-side cursors.

`QuerySet.iterator(chunk_size=...)` opens a named Postgres cursor, which fails with
`InvalidCursorName` behind a pooler running in transaction mode. Keyset pagination over
the primary key gives the same bounded-memory behaviour and works everywhere.
"""


def batched_queryset(queryset, batch_size=500):
    """Yield model instances in primary-key order, `batch_size` at a time."""
    queryset = queryset.order_by("pk")
    last_pk = None

    while True:
        page = queryset
        if last_pk is not None:
            page = page.filter(pk__gt=last_pk)

        rows = list(page[:batch_size])
        if not rows:
            return

        yield from rows
        last_pk = rows[-1].pk


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
        if not rows:
            return

        yield from rows
        last_pk = rows[-1][-1]
