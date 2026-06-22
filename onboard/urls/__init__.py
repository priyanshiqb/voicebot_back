from django.urls import path, include
from .plan import urlpatterns as plan_urls
from .onboarding import urlpatterns as onboarding_urls
from .timezone import urlpatterns as timezone_urls
from .language import urlpatterns as language_urls
from .geography import urlpatterns as geography_urls

urlpatterns = [
    *plan_urls,
    *onboarding_urls,
    *timezone_urls,
    *language_urls,
    *geography_urls,
]
