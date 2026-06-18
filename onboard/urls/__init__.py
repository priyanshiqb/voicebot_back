from django.urls import path, include
from .plan import urlpatterns as plan_urls
from .onboarding import urlpatterns as onboarding_urls
from .timezone import urlpatterns as timezone_urls
from .language import urlpatterns as language_urls
from .postal_code import urlpatterns as postal_code_urls

urlpatterns = [
    *plan_urls,
    *onboarding_urls,
    *timezone_urls,
    *language_urls,
    *postal_code_urls,
]
