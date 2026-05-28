from django.urls import path, include
from .plan import urlpatterns as plan_urls
from .onboarding import urlpatterns as onboarding_urls

urlpatterns = [
    *plan_urls,
    *onboarding_urls,
]
