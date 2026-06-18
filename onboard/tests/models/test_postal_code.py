from django.test import TestCase
from onboard.models.postal_code import PostalCode


class PostalCodeModelTests(TestCase):
    """
    Unit tests for the PostalCode model.
    """

    def test_postal_code_creation_and_fields(self):
        """
        Verify that a PostalCode instance is created with correct fields.
        """
        postal_code = PostalCode.objects.create(
            country_code="AE",
            postal_code="32867 98458",
            place_name="Al Wuheida",
            admin_name1="Dubai",
            admin_code1="03",
            admin_name2="Al Wuheida",
            admin_code2="132",
            latitude=25.293300,
            longitude=55.339900,
            accuracy=6
        )

        self.assertEqual(postal_code.country_code, "AE")
        self.assertEqual(postal_code.postal_code, "32867 98458")
        self.assertEqual(postal_code.place_name, "Al Wuheida")
        self.assertEqual(postal_code.admin_name1, "Dubai")
        self.assertEqual(postal_code.admin_code1, "03")
        self.assertEqual(postal_code.admin_name2, "Al Wuheida")
        self.assertEqual(postal_code.admin_code2, "132")
        self.assertEqual(float(postal_code.latitude), 25.293300)
        self.assertEqual(float(postal_code.longitude), 55.339900)
        self.assertEqual(postal_code.accuracy, 6)
        # Verify coordinates string was generated on save
        self.assertEqual(postal_code.coordinates, "lat: 25.293300, lon: 55.339900")
        # Verify str method output
        self.assertEqual(str(postal_code), "32867 98458 - Al Wuheida (AE)")


