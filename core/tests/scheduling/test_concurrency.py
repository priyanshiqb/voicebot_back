import threading
from unittest import skipUnless

from django.db import connection
from django.test import TransactionTestCase

from core.models.booking import Booking
from core.services import bookings as booking_service
from core.services.exceptions import SlotConflictError
from core.tests.scheduling import factories as f
from core.tests.scheduling.factories import ATTENDEES, NOW, at


@skipUnless(
    connection.vendor == "postgresql",
    "Concurrency guarantee is enforced by the PostgreSQL exclusion constraint.",
)
class BookingConcurrencyTests(TransactionTestCase):
    """
    Two threads race to book the exact same host/slot. The database exclusion
    constraint — not the pre-check — must ensure exactly one wins. The assertion
    holds whether the threads truly interleave (constraint rejects the loser) or
    happen to serialize (soft-check rejects the loser), so it is not flaky.
    """

    RACERS = 6

    def test_concurrent_create_exactly_one_wins(self):
        tenant = f.make_tenant()
        template = f.make_template(tenant)
        host = f.make_host(tenant, template)
        start = at(10)

        results = []
        barrier = threading.Barrier(self.RACERS)

        def worker():
            barrier.wait()
            try:
                booking_service.create_booking(
                    tenant=tenant,
                    event_template=template,
                    start_time=start,
                    participants=ATTENDEES,
                    target_host_id=host.id,
                    now=NOW,
                )
                results.append("ok")
            except SlotConflictError:
                results.append("conflict")
            except Exception as exc:  # noqa: BLE001
                results.append(f"error:{exc!r}")
            finally:
                connection.close()

        threads = [threading.Thread(target=worker) for _ in range(self.RACERS)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        errors = [r for r in results if r.startswith("error")]
        # No racer may crash: deadlocks/serialization failures must be handled,
        # not surfaced as 500s.
        self.assertEqual(errors, [], errors)
        self.assertEqual(results.count("ok"), 1, results)
        self.assertEqual(results.count("conflict"), self.RACERS - 1, results)
        self.assertEqual(
            Booking.objects.filter(
                status=Booking.Status.SCHEDULED, primary_host=host
            ).count(),
            1,
        )
