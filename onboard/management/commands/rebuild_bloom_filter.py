from django.core.management.base import BaseCommand
from django.contrib.auth import get_user_model
from onboard.models import TempUser
from utils.bloom_filter import RedisBloomFilter


class Command(BaseCommand):
    help = "Rebuilds the email Bloom Filter key from active databases."

    def handle(self, *args, **options):
        User = get_user_model()
        bf = RedisBloomFilter()
        bf.clear()

        # Populate all users (including soft-deleted ones)
        active_emails = User.all_objects.values_list('email', flat=True)
        count_active = 0
        for email in active_emails:
            if email:
                bf.add(email)
                count_active += 1

        # Populate pending registrations
        pending_emails = TempUser.objects.values_list('email', flat=True)
        count_pending = 0
        for email in pending_emails:
            if email:
                bf.add(email)
                count_pending += 1

        self.stdout.write(self.style.SUCCESS(
            f"Bloom Filter rebuilt successfully. Added {count_active} active users and {count_pending} pending registrations."
        ))
