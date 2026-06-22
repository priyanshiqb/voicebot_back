import csv
import os
import logging
from django.core.management.base import BaseCommand
from django.db import connection, transaction
from onboard.models import Country, Admin1, Admin2, Admin3, Place

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Unified loader for global geography data from raw CSV"

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
        
        if not csv_path:
            self.stdout.write(self.style.ERROR("Error: You must provide a path to the CSV file using --csv"))
            return
        
        # 1. Safety Check: Skip if database already has data
        if Place.objects.exists() and not force:
            self.stdout.write(
                self.style.WARNING(
                    "Geography data already exists in database. Skipping load.\n"
                    "Use '--force' to truncate the tables and reload."
                )
            )
            return

        # 2. If force, truncate the existing database table
        if force and (Country.objects.exists() or Place.objects.exists()):
            self.stdout.write(self.style.WARNING("Truncating existing geography records..."))
            with connection.cursor() as cursor:
                if connection.vendor == 'sqlite':
                    cursor.execute("DELETE FROM onboard_place;")
                    cursor.execute("DELETE FROM onboard_admin3;")
                    cursor.execute("DELETE FROM onboard_admin2;")
                    cursor.execute("DELETE FROM onboard_admin1;")
                    cursor.execute("DELETE FROM onboard_country;")
                else:
                    cursor.execute("TRUNCATE TABLE onboard_place, onboard_admin3, onboard_admin2, onboard_admin1, onboard_country RESTART IDENTITY CASCADE;")

        # 3. Flow A: Import and validate from raw CSV
        self.import_from_csv(csv_path)

    def import_from_csv(self, csv_path):
        if not os.path.exists(csv_path):
            self.stdout.write(self.style.ERROR(f"CSV file not found: {csv_path}"))
            return

        batch_size = 5000
        batch = []
        count = 0

        self.stdout.write(self.style.WARNING("Building memory caches for geography relations..."))
        country_cache = {c.code: c for c in Country.objects.all()}
        admin1_cache = {(a.country_id, a.name): a for a in Admin1.objects.all()}
        admin2_cache = {(a.admin1_id, a.name): a for a in Admin2.objects.all()}
        admin3_cache = {(a.admin2_id, a.name): a for a in Admin3.objects.all()}
        place_cache = {(p.country_id, p.admin1_id, p.admin2_id, p.admin3_id, p.name, p.postal_code) for p in Place.objects.all()}

        self.stdout.write(self.style.WARNING("Validating and importing raw CSV data..."))

        def get_val(row, key):
            val = row.get(key)
            if val is None:
                val = row.get(key.replace('_', ' '))
            return val

        with open(csv_path, 'r', encoding='utf-8-sig') as f:
            sample = f.readline()
            separator = ';' if ';' in sample else ','
            f.seek(0)
            
            reader = csv.DictReader(f, delimiter=separator)
            
            for row in reader:
                country_code = get_val(row, 'country_code')
                if not country_code:
                    continue
                country_code = country_code.strip()[:10]
                
                # 1. Country
                if country_code not in country_cache:
                    country_name = country_code # Fallback name
                    country_obj = Country.objects.create(code=country_code, name=country_name)
                    country_cache[country_code] = country_obj
                country_obj = country_cache[country_code]

                # 2. Admin 1
                admin1_name = get_val(row, 'admin_name1')
                admin1_code = get_val(row, 'admin_code1')
                admin1_obj = None
                if admin1_name:
                    admin1_name = admin1_name.strip()[:100]
                    cache_key = (country_obj.id, admin1_name)
                    if cache_key not in admin1_cache:
                        admin1_obj = Admin1.objects.create(
                            country=country_obj,
                            code=admin1_code.strip()[:20] if admin1_code else None,
                            name=admin1_name
                        )
                        admin1_cache[cache_key] = admin1_obj
                    admin1_obj = admin1_cache[cache_key]

                # 3. Admin 2
                admin2_name = get_val(row, 'admin_name2')
                admin2_code = get_val(row, 'admin_code2')
                admin2_obj = None
                if admin2_name and admin1_obj:
                    admin2_name = admin2_name.strip()[:100]
                    cache_key = (admin1_obj.id, admin2_name)
                    if cache_key not in admin2_cache:
                        admin2_obj = Admin2.objects.create(
                            admin1=admin1_obj,
                            code=admin2_code.strip()[:20] if admin2_code else None,
                            name=admin2_name
                        )
                        admin2_cache[cache_key] = admin2_obj
                    admin2_obj = admin2_cache[cache_key]

                # 4. Admin 3
                admin3_name = get_val(row, 'admin_name3')
                admin3_code = get_val(row, 'admin_code3')
                admin3_obj = None
                if admin3_name and admin2_obj:
                    admin3_name = admin3_name.strip()[:100]
                    cache_key = (admin2_obj.id, admin3_name)
                    if cache_key not in admin3_cache:
                        admin3_obj = Admin3.objects.create(
                            admin2=admin2_obj,
                            code=admin3_code.strip()[:20] if admin3_code else None,
                            name=admin3_name
                        )
                        admin3_cache[cache_key] = admin3_obj
                    admin3_obj = admin3_cache[cache_key]

                # 5. Place
                place_name = get_val(row, 'place_name')
                postal_code = get_val(row, 'postal_code')
                
                if place_name and postal_code:
                    place_name = place_name.strip()[:180]
                    postal_code = postal_code.strip()[:20]
                    
                    admin1_id = admin1_obj.id if admin1_obj else None
                    admin2_id = admin2_obj.id if admin2_obj else None
                    admin3_id = admin3_obj.id if admin3_obj else None
                    
                    place_key = (country_obj.id, admin1_id, admin2_id, admin3_id, place_name, postal_code)
                    if place_key not in place_cache:
                        place_obj = Place(
                            country=country_obj,
                            admin1=admin1_obj,
                            admin2=admin2_obj,
                            admin3=admin3_obj,
                            name=place_name,
                            postal_code=postal_code
                        )
                        batch.append(place_obj)
                        place_cache.add(place_key)

                if len(batch) >= batch_size:
                    with transaction.atomic():
                        Place.objects.bulk_create(batch, ignore_conflicts=True)
                    count += len(batch)
                    self.stdout.write(f"Validated and imported {count} place rows...")
                    batch = []

            # Remaining items
            if batch:
                with transaction.atomic():
                    Place.objects.bulk_create(batch, ignore_conflicts=True)
                count += len(batch)

        self.stdout.write(self.style.SUCCESS(f"Import complete! {count} clean records saved."))
