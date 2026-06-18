import csv
import gzip
import os
import logging
from django.core.management.base import BaseCommand
from django.db import connection, transaction
from django.conf import settings
from onboard.models.postal_code import PostalCode

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Unified loader for global postal codes from raw CSV or compressed SQL fixtures"

    def add_arguments(self, parser):
        parser.add_argument(
            '--csv',
            type=str,
            help="Path to the raw CSV file (for the first-time validation and import)"
        )
        parser.add_argument(
            '--force',
            action='store_true',
            help="Force reload/overwrite data even if the database already has records"
        )

    def handle(self, *args, **options):
        csv_path = options['csv']
        force = options['force']
        
        # 1. Safety Check: Skip if database already has data
        if PostalCode.objects.exists() and not force:
            self.stdout.write(
                self.style.WARNING(
                    "Postal code data already exists in database. Skipping load.\n"
                    "Use '--force' to truncate the table and reload."
                )
            )
            return

        # 2. If force, truncate the existing database table
        if force and PostalCode.objects.exists():
            self.stdout.write(self.style.WARNING("Truncating existing postal code records..."))
            with connection.cursor() as cursor:
                if connection.vendor == 'sqlite':
                    cursor.execute("DELETE FROM onboard_postal_code;")
                else:
                    cursor.execute("TRUNCATE TABLE onboard_postal_code RESTART IDENTITY CASCADE;")

        # 3. Flow A: Import and validate from raw CSV
        if csv_path:
            self.import_from_csv(csv_path)
            return

        # 4. Flow B: Restore from compressed SQL fixture (default behavior for CI/CD)
        self.restore_from_sql_fixture()

    def import_from_csv(self, csv_path):
        if not os.path.exists(csv_path):
            self.stdout.write(self.style.ERROR(f"CSV file not found: {csv_path}"))
            return

        batch_size = 5000
        batch = []
        count = 0

        self.stdout.write(self.style.WARNING("Validating and importing raw CSV data..."))

        def get_val(row, key):
            """Robust getter that matches keys with either underscores or spaces."""
            val = row.get(key)
            if val is None:
                val = row.get(key.replace('_', ' '))
            return val

        with open(csv_path, 'r', encoding='utf-8-sig') as f:
            # Auto-detect separator (usually ';' or ',')
            sample = f.readline()
            separator = ';' if ';' in sample else ','
            f.seek(0)
            
            reader = csv.DictReader(f, delimiter=separator)
            
            for row in reader:
                # Clean coordinates
                lat_raw = get_val(row, 'latitude')
                lon_raw = get_val(row, 'longitude')
                accuracy_raw = get_val(row, 'accuracy')

                try:
                    latitude = float(lat_raw) if lat_raw else None
                    longitude = float(lon_raw) if lon_raw else None
                except ValueError:
                    latitude, longitude = None, None

                try:
                    accuracy = int(accuracy_raw) if accuracy_raw else None
                except ValueError:
                    accuracy = None

                # Keep coordinates in correct bounds
                if latitude is not None and (latitude < -90 or latitude > 90):
                    latitude = None
                if longitude is not None and (longitude < -180 or longitude > 180):
                    longitude = None

                # Build instance
                postal_code_obj = PostalCode(
                    country_code=get_val(row, 'country_code').strip()[:10] if get_val(row, 'country_code') else '',
                    postal_code=get_val(row, 'postal_code').strip()[:20] if get_val(row, 'postal_code') else '',
                    place_name=get_val(row, 'place_name').strip()[:180] if get_val(row, 'place_name') else '',
                    admin_name1=get_val(row, 'admin_name1').strip()[:100] if get_val(row, 'admin_name1') else None,
                    admin_code1=get_val(row, 'admin_code1').strip()[:20] if get_val(row, 'admin_code1') else None,
                    admin_name2=get_val(row, 'admin_name2').strip()[:100] if get_val(row, 'admin_name2') else None,
                    admin_code2=get_val(row, 'admin_code2').strip()[:20] if get_val(row, 'admin_code2') else None,
                    admin_name3=get_val(row, 'admin_name3').strip()[:100] if get_val(row, 'admin_name3') else None,
                    admin_code3=get_val(row, 'admin_code3').strip()[:20] if get_val(row, 'admin_code3') else None,
                    latitude=latitude,
                    longitude=longitude,
                    accuracy=accuracy,
                    is_active=True
                )
                
                batch.append(postal_code_obj)

                if len(batch) >= batch_size:
                    with transaction.atomic():
                        PostalCode.objects.bulk_create(batch, ignore_conflicts=True)
                    count += len(batch)
                    self.stdout.write(f"Validated and imported {count} rows...")
                    batch = []

            # Remaining items
            if batch:
                with transaction.atomic():
                    PostalCode.objects.bulk_create(batch, ignore_conflicts=True)
                count += len(batch)

        self.stdout.write(self.style.SUCCESS(f"Import complete! {count} clean records saved."))

    def restore_from_sql_fixture(self):
        fixture_path = os.path.join(settings.BASE_DIR, 'onboard', 'fixtures', 'postal_codes.sql.gz')
        
        if not os.path.exists(fixture_path):
            self.stdout.write(
                self.style.ERROR(
                    f"Fixture file not found: {fixture_path}\n"
                    "Please run with '--csv' first to generate your initial data."
                )
            )
            return

        self.stdout.write(self.style.WARNING("Decompressing and seeding from SQL fixture..."))

        # Decompress and read raw SQL queries
        with gzip.open(fixture_path, 'rt', encoding='utf-8') as f:
            sql_data = f.read()

        # Execute the raw inserts inside an atomic database block
        with transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute(sql_data)

        self.stdout.write(self.style.SUCCESS("Database seeded successfully from SQL fixture!"))
