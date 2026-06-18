from django.test import TestCase
from rest_framework.exceptions import ValidationError
from onboard.serializers.postal_code import PostalCodeSerializer


class PostalCodeSerializerTests(TestCase):
    """
    Unit tests for the PostalCodeSerializer validation logic.
    """

    def test_valid_serializer_payload(self):
        """
        Verify that a correct payload passes serializer validation.
        """
        payload = {
            "country_code": "AE",
            "postal_code": "32867 98458",
            "place_name": "Al Wuheida",
            "admin_name1": "Dubai",
            "admin_code1": "03",
            "latitude": 25.293300,
            "longitude": 55.339900,
            "accuracy": 6
        }
        serializer = PostalCodeSerializer(data=payload)
        self.assertTrue(serializer.is_valid())
        validated_data = serializer.validated_data
        self.assertEqual(validated_data["country_code"], "AE")
        self.assertEqual(float(validated_data["latitude"]), 25.2933)

    def test_invalid_latitude_ranges(self):
        """
        Verify that latitudes out of [-90, 90] bounds raise ValidationError.
        """
        # Test too low
        payload = {
            "country_code": "AE",
            "postal_code": "32867",
            "place_name": "Al Wuheida",
            "latitude": -90.1,
            "longitude": 55.3399
        }
        serializer = PostalCodeSerializer(data=payload)
        self.assertFalse(serializer.is_valid())
        self.assertIn("latitude", serializer.errors)

        # Test too high
        payload["latitude"] = 90.0001
        serializer = PostalCodeSerializer(data=payload)
        self.assertFalse(serializer.is_valid())
        self.assertIn("latitude", serializer.errors)

    def test_invalid_longitude_ranges(self):
        """
        Verify that longitudes out of [-180, 180] bounds raise ValidationError.
        """
        # Test too low
        payload = {
            "country_code": "AE",
            "postal_code": "32867",
            "place_name": "Al Wuheida",
            "latitude": 25.2933,
            "longitude": -180.1
        }
        serializer = PostalCodeSerializer(data=payload)
        self.assertFalse(serializer.is_valid())
        self.assertIn("longitude", serializer.errors)

        # Test too high
        payload["longitude"] = 180.0001
        serializer = PostalCodeSerializer(data=payload)
        self.assertFalse(serializer.is_valid())
        self.assertIn("longitude", serializer.errors)

    def test_invalid_accuracy_ranges(self):
        """
        Verify that accuracy out of [1, 6] bounds raises ValidationError.
        """
        payload = {
            "country_code": "AE",
            "postal_code": "32867",
            "place_name": "Al Wuheida",
            "accuracy": 0
        }
        serializer = PostalCodeSerializer(data=payload)
        self.assertFalse(serializer.is_valid())
        self.assertIn("accuracy", serializer.errors)

        payload["accuracy"] = 7
        serializer = PostalCodeSerializer(data=payload)
        self.assertFalse(serializer.is_valid())
        self.assertIn("accuracy", serializer.errors)
