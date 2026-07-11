"""
tasks.py — Celery background tasks
====================================
Covers:
  TASK 5: automatic release of expired seat locks (periodic beat task)
  TASK 2: booking confirmation email, queued + retried, never blocking the
          booking API response
"""
import logging
from celery import shared_task
from django.utils import timezone
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string

from .models import SeatLock, SeatLockStatus, Booking, EmailDeliveryLog

logger = logging.getLogger("booking")


# ----------------------------------------------------------------------
# TASK 5: expired seat lock sweeper
# ----------------------------------------------------------------------
@shared_task
def release_expired_seat_locks():
    """
    Run every 30s via Celery beat (see settings_additions.py). Bulk update
    is used instead of per-row .save() loops — at catalog scale this keeps
    the sweep to one UPDATE statement regardless of how many locks expired.
    """
    now = timezone.now()
    updated = SeatLock.objects.filter(
        status=SeatLockStatus.LOCKED,
        expires_at__lte=now,
    ).update(status=SeatLockStatus.RELEASED)
    if updated:
        logger.info("Released %s expired seat locks", updated)
    return updated


# ----------------------------------------------------------------------
# TASK 2: booking confirmation email
# ----------------------------------------------------------------------
@shared_task(bind=True, max_retries=5, default_retry_delay=30)
def send_booking_confirmation_email(self, booking_id: int):
    """
    Called via `.delay(booking.id)` right after booking is confirmed — the
    view returns its HTTP response immediately without waiting on this.

    Retry strategy: exponential-ish backoff (Celery's default_retry_delay
    combined with `self.retry`) up to 5 attempts. Every attempt — success
    or failure — is logged to EmailDeliveryLog so ops can query "which
    confirmation emails never went out" without grepping logs.
    """
    log, _ = EmailDeliveryLog.objects.get_or_create(booking_id=booking_id)
    log.attempts += 1

    try:
        booking = Booking.objects.select_related("show__movie", "show__theater", "user").get(id=booking_id)
        seat_numbers = ", ".join(booking.seats.values_list("seat_number", flat=True))

        context = {
            "user_name": getattr(booking.user, "first_name", None) or booking.user.username,
            "movie_title": booking.show.movie.title,
            "theater_name": booking.show.theater.name,
            "show_time": booking.show.start_time,
            "seat_numbers": seat_numbers,
            "payment_id": booking.payment.provider_payment_id if hasattr(booking, "payment") else "",
            "total_amount": booking.total_amount,
        }

        html_body = render_to_string("emails/booking_confirmation.html", context)
        text_body = render_to_string("emails/booking_confirmation.txt", context)

        email = EmailMultiAlternatives(
            subject=f"Your tickets for {context['movie_title']} are confirmed",
            body=text_body,
            to=[booking.user.email],
        )
        email.attach_alternative(html_body, "text/html")
        # Do NOT log/store full card or payment-provider secrets anywhere —
        # only the provider_payment_id (a reference token, not sensitive)
        # goes into the template context above.
        email.send(fail_silently=False)

        log.status = "sent"
        log.last_error = ""
        log.save()

    except Exception as exc:  # noqa: BLE001 — deliberately broad: any failure must retry+log
        log.status = "failed"
        log.last_error = str(exc)[:2000]
        log.save()
        logger.error("Booking email failed for booking_id=%s: %s", booking_id, exc)
        raise self.retry(exc=exc)
