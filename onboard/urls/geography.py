from django.urls import path
from onboard.views import (
    CountryDropdownAPIView,
    Admin1DropdownAPIView,
    Admin2DropdownAPIView,
    Admin3DropdownAPIView,
    PlaceDropdownAPIView,
    PlacePostalCodeDropdownAPIView,
    PlaceDetailAPIView
)

urlpatterns = [
    path('countries/dropdown/', CountryDropdownAPIView.as_view(), name='country-dropdown'),
    path('admin1/dropdown/', Admin1DropdownAPIView.as_view(), name='admin1-dropdown'),
    path('admin2/dropdown/', Admin2DropdownAPIView.as_view(), name='admin2-dropdown'),
    path('admin3/dropdown/', Admin3DropdownAPIView.as_view(), name='admin3-dropdown'),
    path('places/dropdown/', PlaceDropdownAPIView.as_view(), name='place-dropdown'),
    path('places/postal-codes/dropdown/', PlacePostalCodeDropdownAPIView.as_view(), name='place-postal-code-dropdown'),
    path('places/<uuid:pk>/', PlaceDetailAPIView.as_view(), name='place-detail'),
]
