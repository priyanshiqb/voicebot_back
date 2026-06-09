from .custom_user import urlpatterns as custom_user_urls
from .auth import urlpatterns as auth_urls
from .session import urlpatterns as session_urls
from django.urls import path, include

urlpatterns = [
    *custom_user_urls,
    *auth_urls,
    *session_urls,
]