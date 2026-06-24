import csv
import os
import logging
from django.core.management.base import BaseCommand
from django.db import connection, transaction
from onboard.models import Country, Admin1, Admin2, Admin3, Place

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Unified loader for global geography data from raw CSV"

    COUNTRY_MAPPING = {
        'PT': 'Portugal', 'AE': 'United Arab Emirates', 'IN': 'India', 'JP': 'Japan',
        'MX': 'Mexico', 'SG': 'Singapore', 'PE': 'Peru', 'ID': 'Indonesia',
        'PL': 'Poland', 'FR': 'France', 'RU': 'Russia', 'US': 'United States',
        'RO': 'Romania', 'ES': 'Spain', 'TR': 'Turkey', 'KR': 'South Korea',
        'UA': 'Ukraine', 'GB': 'United Kingdom', 'DE': 'Germany', 'LT': 'Lithuania',
        'AR': 'Argentina', 'AT': 'Austria', 'SE': 'Sweden', 'AU': 'Australia',
        'IT': 'Italy', 'DZ': 'Algeria', 'CZ': 'Czech Republic', 'HR': 'Croatia',
        'LV': 'Latvia', 'BR': 'Brazil', 'EE': 'Estonia', 'BG': 'Bulgaria',
        'SK': 'Slovakia', 'NO': 'Norway', 'LU': 'Luxembourg', 'CH': 'Switzerland',
        'NL': 'Netherlands', 'ZA': 'South Africa', 'CO': 'Colombia', 'FI': 'Finland',
        'HU': 'Hungary', 'BY': 'Belarus', 'MY': 'Malaysia', 'BE': 'Belgium',
        'PK': 'Pakistan', 'CN': 'China', 'PH': 'Philippines', 'UY': 'Uruguay',
        'LK': 'Sri Lanka', 'MD': 'Moldova', 'NZ': 'New Zealand', 'CA': 'Canada',
        'BD': 'Bangladesh', 'MA': 'Morocco', 'EC': 'Ecuador', 'AZ': 'Azerbaijan',
        'DK': 'Denmark', 'RS': 'Serbia', 'CY': 'Cyprus', 'TH': 'Thailand',
        'KE': 'Kenya', 'PA': 'Panama', 'SI': 'Slovenia', 'GT': 'Guatemala',
        'DO': 'Dominican Republic', 'AL': 'Albania', 'MW': 'Malawi', 'CR': 'Costa Rica',
        'CL': 'Chile', 'HT': 'Haiti', 'MK': 'North Macedonia', 'PF': 'French Polynesia',
        'IS': 'Iceland', 'PR': 'Puerto Rico', 'RE': 'Réunion', 'IE': 'Ireland',
        'FO': 'Faroe Islands', 'BM': 'Bermuda', 'GP': 'Guadeloupe', 'MQ': 'Martinique',
        'IM': 'Isle of Man', 'GF': 'French Guiana', 'MT': 'Malta', 'NC': 'New Caledonia',
        'HN': 'Honduras', 'AX': 'Åland Islands', 'GL': 'Greenland', 'MC': 'Monaco',
        'SM': 'San Marino', 'GU': 'Guam', 'YT': 'Mayotte', 'VI': 'United States Virgin Islands',
        'GG': 'Guernsey', 'LI': 'Liechtenstein', 'SJ': 'Svalbard and Jan Mayen',
        'AD': 'Andorra', 'JE': 'Jersey', 'FM': 'Micronesia', 'MP': 'Northern Mariana Islands',
        'WF': 'Wallis and Futuna', 'MH': 'Marshall Islands', 'PM': 'Saint Pierre and Miquelon',
        'AI': 'Anguilla', 'AS': 'American Samoa', 'CC': 'Cocos (Keeling) Islands',
        'CX': 'Christmas Island', 'FK': 'Falkland Islands', 'GI': 'Gibraltar',
        'GS': 'South Georgia and the South Sandwich Islands', 'HK': 'Hong Kong',
        'HM': 'Heard Island and McDonald Islands', 'IO': 'British Indian Ocean Territory',
        'MO': 'Macao', 'NF': 'Norfolk Island', 'NR': 'Nauru', 'NU': 'Niue',
        'PN': 'Pitcairn Islands', 'PW': 'Palau', 'TC': 'Turks and Caicos Islands',
        'VA': 'Vatican City', 'WS': 'Samoa'
    }

    PHONE_DATA = {
        'PT': {'dial_code': '+351', 'format_mask': 'XXX XXX XXX'},
        'AE': {'dial_code': '+971', 'format_mask': 'XX XXX XXXX'},
        'IN': {'dial_code': '+91', 'format_mask': 'XXXXX XXXXX'},
        'JP': {'dial_code': '+81', 'format_mask': 'XX-XXXX-XXXX'},
        'MX': {'dial_code': '+52', 'format_mask': 'XXX XXX XXXX'},
        'SG': {'dial_code': '+65', 'format_mask': 'XXXX XXXX'},
        'PE': {'dial_code': '+51', 'format_mask': 'XXX XXX XXX'},
        'ID': {'dial_code': '+62', 'format_mask': 'XXX-XXX-XXX'},
        'PL': {'dial_code': '+48', 'format_mask': 'XXX XXX XXX'},
        'FR': {'dial_code': '+33', 'format_mask': 'X XX XX XX XX'},
        'RU': {'dial_code': '+7', 'format_mask': 'XXX XXX-XX-XX'},
        'US': {'dial_code': '+1', 'format_mask': 'XXX-XXX-XXXX'},
        'RO': {'dial_code': '+40', 'format_mask': 'XXX XXX XXX'},
        'ES': {'dial_code': '+34', 'format_mask': 'XXX XX XX XX'},
        'TR': {'dial_code': '+90', 'format_mask': 'XXX XXX XX XX'},
        'KR': {'dial_code': '+82', 'format_mask': 'XX-XXXX-XXXX'},
        'UA': {'dial_code': '+380', 'format_mask': 'XX XXX XXXX'},
        'GB': {'dial_code': '+44', 'format_mask': 'XXXX XXXXXX'},
        'DE': {'dial_code': '+49', 'format_mask': 'XXXX XXXXXXX'},
        'LT': {'dial_code': '+370', 'format_mask': 'XXX XXXXX'},
        'AR': {'dial_code': '+54', 'format_mask': 'X XX XXXX-XXXX'},
        'AT': {'dial_code': '+43', 'format_mask': 'XXX XXXXXX'},
        'SE': {'dial_code': '+46', 'format_mask': 'XX XXX XX XX'},
        'AU': {'dial_code': '+61', 'format_mask': 'XXX XXX XXX'},
        'IT': {'dial_code': '+39', 'format_mask': 'XXX XXX XXXX'},
        'DZ': {'dial_code': '+213', 'format_mask': 'XXX XX XX XX'},
        'CZ': {'dial_code': '+420', 'format_mask': 'XXX XXX XXX'},
        'HR': {'dial_code': '+385', 'format_mask': 'XX XXX XXXX'},
        'LV': {'dial_code': '+371', 'format_mask': 'XX XXX XXX'},
        'BR': {'dial_code': '+55', 'format_mask': 'XX XXXXX-XXXX'},
        'EE': {'dial_code': '+372', 'format_mask': 'XXXX XXXX'},
        'BG': {'dial_code': '+359', 'format_mask': 'XX XXX XXX'},
        'SK': {'dial_code': '+421', 'format_mask': 'XXX XXX XXX'},
        'NO': {'dial_code': '+47', 'format_mask': 'XX XX XX XX'},
        'LU': {'dial_code': '+352', 'format_mask': 'XXX XXX XXX'},
        'CH': {'dial_code': '+41', 'format_mask': 'XX XXX XX XX'},
        'NL': {'dial_code': '+31', 'format_mask': 'X XXXXXXXX'},
        'ZA': {'dial_code': '+27', 'format_mask': 'XX XXX XXXX'},
        'CO': {'dial_code': '+57', 'format_mask': 'XXX XXXXXXX'},
        'FI': {'dial_code': '+358', 'format_mask': 'XX XXXXXXX'},
        'HU': {'dial_code': '+36', 'format_mask': 'XX XXX XXXX'},
        'BY': {'dial_code': '+375', 'format_mask': 'XX XXX-XX-XX'},
        'MY': {'dial_code': '+60', 'format_mask': 'XX-XXX XXXX'},
        'BE': {'dial_code': '+32', 'format_mask': 'XXX XX XX XX'},
        'PK': {'dial_code': '+92', 'format_mask': 'XXX XXXXXXX'},
        'CN': {'dial_code': '+86', 'format_mask': 'XXX XXXX XXXX'},
        'PH': {'dial_code': '+63', 'format_mask': 'XXX XXX XXXX'},
        'UY': {'dial_code': '+598', 'format_mask': 'XX XXX XXX'},
        'LK': {'dial_code': '+94', 'format_mask': 'XX XXX XXXX'},
        'MD': {'dial_code': '+373', 'format_mask': 'XXX XX XXX'},
        'NZ': {'dial_code': '+64', 'format_mask': 'XX XXX XXXX'},
        'CA': {'dial_code': '+1', 'format_mask': 'XXX-XXX-XXXX'},
        'BD': {'dial_code': '+880', 'format_mask': 'XXXX-XXXXXX'},
        'MA': {'dial_code': '+212', 'format_mask': 'X XX XX XX XX'},
        'EC': {'dial_code': '+593', 'format_mask': 'XX XXX XXXX'},
        'AZ': {'dial_code': '+994', 'format_mask': 'XX XXX XX XX'},
        'DK': {'dial_code': '+45', 'format_mask': 'XX XX XX XX'},
        'RS': {'dial_code': '+381', 'format_mask': 'XX XXXXXXX'},
        'CY': {'dial_code': '+357', 'format_mask': 'XX XXXXXX'},
        'TH': {'dial_code': '+66', 'format_mask': 'XX XXX XXXX'},
        'KE': {'dial_code': '+254', 'format_mask': 'XXX XXXXXX'},
        'PA': {'dial_code': '+507', 'format_mask': 'XXXX-XXXX'},
        'SI': {'dial_code': '+386', 'format_mask': 'XX XXX XXX'},
        'GT': {'dial_code': '+502', 'format_mask': 'XXXX XXXX'},
        'DO': {'dial_code': '+1', 'format_mask': 'XXX-XXX-XXXX'},
        'AL': {'dial_code': '+355', 'format_mask': 'XX XXX XXXX'},
        'MW': {'dial_code': '+265', 'format_mask': 'XXX XX XX XX'},
        'CR': {'dial_code': '+506', 'format_mask': 'XXXX XXXX'},
        'CL': {'dial_code': '+56', 'format_mask': 'X XXXX XXXX'},
        'HT': {'dial_code': '+509', 'format_mask': 'XX XX XXXX'},
        'MK': {'dial_code': '+389', 'format_mask': 'XX XXX XXX'},
        'PF': {'dial_code': '+689', 'format_mask': 'XX XX XX XX'},
        'IS': {'dial_code': '+354', 'format_mask': 'XXX XXXX'},
        'PR': {'dial_code': '+1', 'format_mask': 'XXX-XXX-XXXX'},
        'RE': {'dial_code': '+262', 'format_mask': 'XXX XX XX XX'},
        'IE': {'dial_code': '+353', 'format_mask': 'XX XXX XXXX'},
        'FO': {'dial_code': '+298', 'format_mask': 'XXXXXX'},
        'BM': {'dial_code': '+1', 'format_mask': 'XXX-XXX-XXXX'},
        'GP': {'dial_code': '+590', 'format_mask': 'XXX XX XX XX'},
        'MQ': {'dial_code': '+596', 'format_mask': 'XXX XX XX XX'},
        'IM': {'dial_code': '+44', 'format_mask': 'XXXX XXXXXX'},
        'GF': {'dial_code': '+594', 'format_mask': 'XXX XX XX XX'},
        'MT': {'dial_code': '+356', 'format_mask': 'XXXX XXXX'},
        'NC': {'dial_code': '+687', 'format_mask': 'XX.XX.XX'},
        'HN': {'dial_code': '+504', 'format_mask': 'XXXX-XXXX'},
        'AX': {'dial_code': '+358', 'format_mask': 'XX XXXXXXX'},
        'GL': {'dial_code': '+299', 'format_mask': 'XX XX XX'},
        'MC': {'dial_code': '+377', 'format_mask': 'X XX XX XX XX'},
        'SM': {'dial_code': '+378', 'format_mask': 'XX XX XX XX'},
        'GU': {'dial_code': '+1', 'format_mask': 'XXX-XXX-XXXX'},
        'YT': {'dial_code': '+262', 'format_mask': 'XXX XX XX XX'},
        'VI': {'dial_code': '+1', 'format_mask': 'XXX-XXX-XXXX'},
        'GG': {'dial_code': '+44', 'format_mask': 'XXXX XXXXXX'},
        'LI': {'dial_code': '+423', 'format_mask': 'XXX XXX XXX'},
        'SJ': {'dial_code': '+47', 'format_mask': 'XX XX XX XX'},
        'AD': {'dial_code': '+376', 'format_mask': 'XXX XXX'},
        'JE': {'dial_code': '+44', 'format_mask': 'XXXX XXXXXX'},
        'FM': {'dial_code': '+691', 'format_mask': 'XXX XXXX'},
        'MP': {'dial_code': '+1', 'format_mask': 'XXX-XXX-XXXX'},
        'WF': {'dial_code': '+681', 'format_mask': 'XX XX XX'},
        'MH': {'dial_code': '+692', 'format_mask': 'XXX-XXXX'},
        'PM': {'dial_code': '+508', 'format_mask': 'XX XX XX'},
        'AI': {'dial_code': '+1', 'format_mask': 'XXX-XXX-XXXX'},
        'AS': {'dial_code': '+1', 'format_mask': 'XXX-XXX-XXXX'},
        'CC': {'dial_code': '+61', 'format_mask': 'XXX XXX XXX'},
        'CX': {'dial_code': '+61', 'format_mask': 'XXX XXX XXX'},
        'FK': {'dial_code': '+500', 'format_mask': 'XXXXX'},
        'GI': {'dial_code': '+350', 'format_mask': 'XXXXXXXX'},
        'HK': {'dial_code': '+852', 'format_mask': 'XXXX XXXX'},
        'IO': {'dial_code': '+246', 'format_mask': 'XXX XXXX'},
        'MO': {'dial_code': '+853', 'format_mask': 'XXXX XXXX'},
        'NF': {'dial_code': '+672', 'format_mask': 'X XXXXX'},
        'NR': {'dial_code': '+674', 'format_mask': 'XXX XXXX'},
        'NU': {'dial_code': '+683', 'format_mask': 'XXX XXXX'},
        'PW': {'dial_code': '+680', 'format_mask': 'XXX XXXX'},
        'TC': {'dial_code': '+1', 'format_mask': 'XXX-XXX-XXXX'},
        'VA': {'dial_code': '+39', 'format_mask': 'XXX XXX XXXX'},
        'WS': {'dial_code': '+685', 'format_mask': 'XX XXXXX'},
    }

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
                    country_name = self.COUNTRY_MAPPING.get(country_code, country_code)
                    phone_info = self.PHONE_DATA.get(country_code, {})
                    country_obj = Country.objects.create(
                        code=country_code, 
                        name=country_name,
                        dial_code=phone_info.get('dial_code'),
                        format_mask=phone_info.get('format_mask')
                    )
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
