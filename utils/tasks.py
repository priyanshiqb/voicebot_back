import logging
from celery import shared_task
from django.core.mail import send_mail
from django.template.loader import render_to_string
from django.utils import timezone

logger = logging.getLogger(__name__)


@shared_task
def send_email_task(
    subject,
    message,
    from_email,
    recipient_list,
    fail_silently=False,
    html_message=None,
    title=None,
    lead_text=None,
    otp_code=None,
    body_text=None,
):
    """
    Celery task to send emails asynchronously.
    Supports rendering a generic, modern HTML email template.
    """
    logger.info(f"Sending email task triggered for: {recipient_list} with subject '{subject}'")

    if not html_message and (title or lead_text or otp_code):
        try:
            context = {
                "subject": subject,
                "title": title or subject,
                "lead_text": lead_text or message,
                "otp_code": otp_code,
                "body_text": body_text,
                "current_year": timezone.now().year,
            }
            html_message = render_to_string("emails/generic_email.html", context)
        except Exception as e:
            logger.exception("Failed to render HTML email template: %s", str(e))

    try:
        send_mail(
            subject=subject,
            message=message,
            from_email=from_email,
            recipient_list=recipient_list,
            fail_silently=fail_silently,
            html_message=html_message,
        )
        logger.info(f"Email successfully sent to: {recipient_list}")
    except Exception as e:
        logger.error(f"Email task failed for: {recipient_list}. Error: {str(e)}")
        if not fail_silently:
            raise e

