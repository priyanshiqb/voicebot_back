from django.apps import AppConfig


class OnboardConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'onboard'

    def ready(self):
        import onboard.signals
        
        # Asynchronously update Bloom Filter on project start
        import sys
        import logging
        import threading
        from django.db import connection, close_old_connections

        logger = logging.getLogger(__name__)

        def rebuild_bloom_filter_async():
            try:
                from django.apps import apps
                import time
                
                # Wait until Django is fully initialized to avoid database RuntimeWarning
                while not apps.ready:
                    time.sleep(0.1)

                from django.contrib.auth import get_user_model
                from onboard.models import TempUser
                from utils.bloom_filter import RedisBloomFilter

                # Check if tables exist before querying
                db_tables = connection.introspection.table_names()
                User = get_user_model()
                if User._meta.db_table in db_tables and TempUser._meta.db_table in db_tables:
                    bf = RedisBloomFilter()
                    bf.clear()
                    
                    # Populate all users (including soft-deleted ones to avoid false negatives on unavailable emails)
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
                    
                    logger.info(
                        f"Bloom Filter successfully updated on project start. "
                        f"Loaded {count_active} users and {count_pending} pending registrations."
                    )
            except Exception as e:
                logger.warning(f"Failed to auto-update Bloom Filter on project start: {e}")
            finally:
                close_old_connections()

        # Skip initialization during migrations, test runs, or static collection
        skip_commands = {'migrate', 'makemigrations', 'collectstatic', 'test', 'showmigrations'}
        if not any(arg in skip_commands for arg in sys.argv):
            threading.Thread(target=rebuild_bloom_filter_async, daemon=True).start()
