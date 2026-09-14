from django.apps import AppConfig


class OnboardConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'onboard'

    def ready(self):
        # Deploy-time validation for payment settings that fail silently.
        import onboard.checks  # noqa: F401
