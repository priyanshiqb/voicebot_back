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
    delivery_log_id=None,
):
    """
    Celery task to send emails asynchronously.
    Supports rendering a generic, modern HTML email template.

    ``delivery_log_id`` points at an ``EmailDeliveryLog`` row opened by the caller;
    this task writes the delivery outcome onto it. The row is created at the call
    site because only the request knows who asked and from where, and marked here
    because only the worker knows whether the send succeeded.

    ``otp_code`` is rendered into the message and **never recorded** — a table of
    live one-time codes would be a credential store.
    """
    from user.services import email_log
    from user.models.email_delivery_log import EmailDeliveryLog

    # Recipients are deliberately absent from this line. They are personal data, and
    # they now live in a table with a retention policy instead of in application
    # logs that have neither retention nor a way to be queried.
    logger.info(
        "Sending email task triggered", extra={"delivery_log_id": delivery_log_id}
    )

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
        email_log.mark(delivery_log_id, EmailDeliveryLog.Status.SENT)
        logger.info("Email sent", extra={"delivery_log_id": delivery_log_id})
    except Exception as e:
        email_log.mark(delivery_log_id, EmailDeliveryLog.Status.FAILED, error=str(e))
        logger.error(
            "Email task failed: %s", str(e), extra={"delivery_log_id": delivery_log_id}
        )
        if not fail_silently:
            raise e

