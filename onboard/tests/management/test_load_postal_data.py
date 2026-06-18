import csv
import os
import tempfile
from io import StringIO
from django.core.management import call_command
from django.test import TestCase
from onboard.models.postal_code import PostalCode


class LoadPostalDataCommandTests(TestCase):
    def setUp(self):
        # Clean the DB table before each test
        PostalCode.objects.all().delete()

    def test_csv_import_success(self):
        # Create a mock CSV with headers containing spaces and semicolons as separators
        csv_data = (
            "country code;postal code;place name;admin name1;admin code1;admin name2;admin code2;admin name3;admin code3;latitude;longitude;accuracy;coordinates\n"
            "AE;32867 98458;Al Wuheida;;03;;100;Al Wuheida;132;25.2933;55.3399;6;25.2933, 55.3399\n"
            "AE;32888 96825;Hor Al Anz;;03;;100;Hor Al Anz;127;25.2785;55.3403;6;25.2785, 55.3403\n"
        )

        with tempfile.NamedTemporaryFile(mode='w', suffix='.csv', delete=False, encoding='utf-8') as temp_file:
            temp_file.write(csv_data)
            temp_file_path = temp_file.name

        try:
            out = StringIO()
            call_command('load_postal_data', csv=temp_file_path, stdout=out)
            output = out.getvalue()

            self.assertIn("Import complete!", output)
            self.assertEqual(PostalCode.objects.count(), 2)

            # Check correctness of saved fields
            pc1 = PostalCode.objects.get(postal_code="32867 98458")
            self.assertEqual(pc1.country_code, "AE")
            self.assertEqual(pc1.place_name, "Al Wuheida")
            self.assertEqual(pc1.admin_code1, "03")
            self.assertEqual(pc1.admin_name3, "Al Wuheida")
            self.assertEqual(float(pc1.latitude), 25.2933)
            self.assertEqual(float(pc1.longitude), 55.3399)
            self.assertEqual(pc1.accuracy, 6)
            self.assertEqual(pc1.coordinates, "lat: 25.293300, lon: 55.339900")
            self.assertTrue(pc1.is_active)
        finally:
            os.remove(temp_file_path)

    def test_skip_when_data_exists(self):
        # Seed one record
        PostalCode.objects.create(
            country_code="AE",
            postal_code="12345",
            place_name="Test Area"
        )
        self.assertEqual(PostalCode.objects.count(), 1)

        # Run command without force should skip
        out = StringIO()
        call_command('load_postal_data', stdout=out)
        output = out.getvalue()

        self.assertIn("Postal code data already exists in database. Skipping load.", output)
        self.assertEqual(PostalCode.objects.count(), 1)

    def test_force_reload_truncates_and_seeds(self):
        # Seed a dummy record
        PostalCode.objects.create(
            country_code="AE",
            postal_code="12345",
            place_name="Test Area"
        )
        self.assertEqual(PostalCode.objects.count(), 1)

        # Mock CSV to load via force
        csv_data = (
            "country code;postal code;place name;admin name1;admin code1;admin name2;admin code2;admin name3;admin code3;latitude;longitude;accuracy;coordinates\n"
            "AE;32867 98458;Al Wuheida;;03;;100;Al Wuheida;132;25.2933;55.3399;6;25.2933, 55.3399\n"
        )

        with tempfile.NamedTemporaryFile(mode='w', suffix='.csv', delete=False, encoding='utf-8') as temp_file:
            temp_file.write(csv_data)
            temp_file_path = temp_file.name

        try:
            out = StringIO()
            call_command('load_postal_data', csv=temp_file_path, force=True, stdout=out)
            output = out.getvalue()

            self.assertIn("Truncating existing postal code records...", output)
            self.assertIn("Import complete!", output)
            # The count should be exactly 1 (from CSV), and the "12345" one should be truncated
            self.assertEqual(PostalCode.objects.count(), 1)
            self.assertFalse(PostalCode.objects.filter(postal_code="12345").exists())
            self.assertTrue(PostalCode.objects.filter(postal_code="32867 98458").exists())
        finally:
            os.remove(temp_file_path)
