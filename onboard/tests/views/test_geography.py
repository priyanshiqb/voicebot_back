import uuid
from django.test import TestCase
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient
from user.models import CustomUser

from onboard.models import Country, Admin1, Admin2, Admin3, Place

class GeographyAPITests(TestCase):
    def setUp(self):
        self.client = APIClient()
        # Create a user to bypass IsAuthenticated
        self.user = CustomUser.objects.create(email="test@voicebot.com", password="password")
        self.client.force_authenticate(user=self.user)
        
        self.country = Country.objects.create(code="IN", name="India")
        self.country_empty = Country.objects.create(code="XX", name="Empty Country")
        
        self.admin1 = Admin1.objects.create(country=self.country, code="GJ", name="Gujarat")
        self.admin1_empty = Admin1.objects.create(country=self.country, code="MH", name="Maharashtra")
        
        self.admin2 = Admin2.objects.create(admin1=self.admin1, code="SR", name="Surat")
        self.admin2_empty = Admin2.objects.create(admin1=self.admin1, code="AH", name="Ahmedabad")
        
        self.admin3 = Admin3.objects.create(admin2=self.admin2, code="VD", name="Varachha")
        
        self.place = Place.objects.create(
            country=self.country,
            admin1=self.admin1,
            admin2=self.admin2,
            admin3=self.admin3,
            postal_code="395 006",
            name="Varachha Road"
        )
        self.place2 = Place.objects.create(
            country=self.country,
            postal_code="395007",
            name="Other Place"
        )

    def test_country_dropdown(self):
        url = reverse('api-v1:country-dropdown')
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['status'], True)
        self.assertEqual(len(response.data['data']), 2)
        
    def test_admin1_dropdown_no_further_data(self):
        url = reverse('api-v1:admin1-dropdown')
        response = self.client.get(url, {'country_id': str(self.country_empty.id)})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data['errors']['code'], 'NO_FURTHER_DATA')
        
    def test_admin1_dropdown_success(self):
        url = reverse('api-v1:admin1-dropdown')
        response = self.client.get(url, {'country_id': str(self.country.id)})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data['data']), 2)
        
    def test_admin2_dropdown_no_further_data(self):
        url = reverse('api-v1:admin2-dropdown')
        response = self.client.get(url, {'admin1_id': str(self.admin1_empty.id)})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data['errors']['code'], 'NO_FURTHER_DATA')
        
    def test_admin3_dropdown_no_further_data(self):
        url = reverse('api-v1:admin3-dropdown')
        response = self.client.get(url, {'admin2_id': str(self.admin2_empty.id)})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data['errors']['code'], 'NO_FURTHER_DATA')
        
    def test_place_dropdown_search_postal_space(self):
        url = reverse('api-v1:place-dropdown')
        # Search with space ignored
        response = self.client.get(url, {'search': '395006'})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data['data']), 1)
        self.assertEqual(response.data['data'][0]['cities'], "Varachha Road")
        
        # Search by name
        response = self.client.get(url, {'search': 'Varachha'})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data['data']), 1)
        
    def test_place_dropdown_filters(self):
        url = reverse('api-v1:place-dropdown')
        response = self.client.get(url, {'admin1_id': str(self.admin1.id)})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data['data']), 1)

    def test_place_postal_code_dropdown(self):
        """
        The dropdown returns {value: <Place uuid>, label: "<name> | <postal_code>"} —
        the postal code is in the label, not the value. See
        docs/apiDocs/onboard/postal_code_dropdown.md.
        """
        url = reverse('api-v1:place-postal-code-dropdown')

        # Search ignoring space
        response = self.client.get(url, {'search': '395006'})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data['data']), 1)
        self.assertEqual(response.data['data'][0]['value'], str(self.place.id))
        self.assertEqual(response.data['data'][0]['label'], "Varachha Road | 395 006")

        # Exact match
        response = self.client.get(url, {'postal_code': '395007'})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data['data']), 1)
        self.assertEqual(response.data['data'][0]['value'], str(self.place2.id))
        self.assertEqual(response.data['data'][0]['label'], "Other Place | 395007")

    def test_place_detail(self):
        url = reverse('api-v1:place-detail', kwargs={'pk': self.place.id})
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        
        data = response.data['data']
        self.assertEqual(data['id'], str(self.place.id))
        self.assertEqual(data['postal_code'], "395 006")
        
        # Check nested structures (HybridRelatedField)
        self.assertIn('country', data)
        self.assertEqual(data['country']['code'], "IN")
        
        self.assertIn('admin1', data)
        self.assertEqual(data['admin1']['code'], "GJ")
        
        self.assertIn('admin2', data)
        self.assertEqual(data['admin2']['code'], "SR")
        
        self.assertIn('admin3', data)
        self.assertEqual(data['admin3']['code'], "VD")
