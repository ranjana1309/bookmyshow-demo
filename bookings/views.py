"""
bookings/views.py — all six feature implementations, in one file, so the
app has ONE views module and ONE urls.py (see urls.py) instead of scattered
files. Sections are marked clearly; each still maps 1:1 to a task.
"""
import json
import logging
import re

import stripe
from django.conf import settings
from django.contrib.auth.decorators import login_required, user_passes_test
from django.core.cache import cache
from django.core.paginator import Paginator
from django.db import transaction, IntegrityError
from django.db.models import (
    Sum, Count, F, Q, FloatField, ExpressionWrapper,
)
from django.db.models.functions import TruncDay, ExtractHour
from django.http import JsonResponse, HttpResponse
from django.shortcuts import get_object_or_404
from django.views import View
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from django.utils import timezone
from datetime import timedelta

from .models import (
    Movie, Genre, Language, Show, Seat, SeatLock, SeatLockStatus,
    Booking, Payment, WebhookEvent, Theater,
)
from .tasks import send_booking_confirmation_email

logger = logging.getLogger("bookings")
stripe.api_key = getattr(settings, "STRIPE_SECRET_KEY", None)


# ============================================================================
# TASK 1 — Scalable Genre/Language Filtering with Query Optimization
# ============================================================================
ALLOWED_SORT_FIELDS = {
    "release_date": "release_date",
    "-release_date": "-release_date",
    "title": "title",
    "-title": "-title",
}


def _filter_movies(genre_ids=None, language_ids=None, sort="-release_date", page=1, page_size=20):
    """
    Multi-select filters use `__in` (one indexed query), not one query per
    selected value. `.distinct()` runs at the DB level because filtering
    across a M2M can otherwise duplicate rows. `.only()` avoids pulling
    unused columns. Sort field is whitelisted to stop arbitrary column
    injection via query params. See models.py for the composite index
    (is_active, -release_date) this query relies on.
    """
    qs = Movie.objects.filter(is_active=True)
    if genre_ids:
        qs = qs.filter(genres__id__in=genre_ids).distinct()
    if language_ids:
        qs = qs.filter(languages__id__in=language_ids).distinct()

    qs = qs.order_by(ALLOWED_SORT_FIELDS.get(sort, "-release_date"))
    qs = qs.only("id", "title", "release_date", "poster_url")

    paginator = Paginator(qs, page_size)
    page_obj = paginator.get_page(page)
    return {
        "results": list(page_obj.object_list),
        "page": page_obj.number,
        "total_pages": paginator.num_pages,
        "total_count": paginator.count,
    }


def _dynamic_filter_counts(genre_ids=None, language_ids=None, cache_seconds=30):
    """
    Single aggregated query per facet (not one query per genre/language) —
    counts respect the OTHER active filter, which is what makes them
    "dynamic". Cached briefly since these change slowly relative to
    request volume.
    """
    cache_key = f"filter_counts:g={sorted(genre_ids or [])}:l={sorted(language_ids or [])}"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    base = Movie.objects.filter(is_active=True)
    if language_ids:
        base = base.filter(languages__id__in=language_ids)
    genre_counts = (
        Genre.objects.filter(movies__in=base)
        .annotate(count=Count("movies", filter=Q(movies__in=base), distinct=True))
        .values("id", "name", "count").order_by("-count")
    )

    base_lang = Movie.objects.filter(is_active=True)
    if genre_ids:
        base_lang = base_lang.filter(genres__id__in=genre_ids)
    language_counts = (
        Language.objects.filter(movies__in=base_lang)
        .annotate(count=Count("movies", filter=Q(movies__in=base_lang), distinct=True))
        .values("id", "name", "count").order_by("-count")
    )

    result = {"genres": list(genre_counts), "languages": list(language_counts)}
    cache.set(cache_key, result, cache_seconds)
    return result


class MovieListView(View):
    """GET /movies/?genre=1&genre=2&language=3&sort=-release_date&page=1"""

    def get(self, request):
        genre_ids = [int(g) for g in request.GET.getlist("genre") if g.isdigit()]
        language_ids = [int(l) for l in request.GET.getlist("language") if l.isdigit()]
        sort = request.GET.get("sort", "-release_date")
        page = int(request.GET.get("page", 1))
        page_size = min(int(request.GET.get("page_size", 20)), 100)

        data = _filter_movies(genre_ids, language_ids, sort, page, page_size)
        counts = _dynamic_filter_counts(genre_ids, language_ids)

        return JsonResponse({
            "movies": [
                {"id": m.id, "title": m.title, "release_date": m.release_date.isoformat(),
                 "poster_url": m.poster_url}
                for m in data["results"]
            ],
            "page": data["page"], "total_pages": data["total_pages"],
            "total_count": data["total_count"], "filter_counts": counts,
        })


# ============================================================================
# TASK 3 — Secure YouTube Trailer Embedding
# ============================================================================
YOUTUBE_ID_RE = re.compile(
    r"""(?:youtube\.com/(?:watch\?v=|embed/|v/|shorts/)|youtu\.be/)([A-Za-z0-9_-]{11})"""
)


def extract_youtube_id(url):
    """Only an 11-char, regex-constrained video ID is ever extracted — no
    attacker-controlled string reaches the template, so this sidesteps
    XSS structurally rather than by escaping."""
    if not url or len(url) > 500:
        return None
    match = YOUTUBE_ID_RE.search(url.strip())
    return match.group(1) if match else None


def movie_detail(request, movie_id):
    movie = get_object_or_404(Movie, id=movie_id, is_active=True)
    video_id = extract_youtube_id(movie.trailer_url)
    trailer_context = (
        {"trailer_available": True, "embed_url": f"https://www.youtube-nocookie.com/embed/{video_id}"}
        if video_id else {"trailer_available": False}
    )
    return JsonResponse({
        "id": movie.id, "title": movie.title, "poster_url": movie.poster_url,
        **trailer_context,
    })


# ============================================================================
# TASK 5 — Concurrency-Safe Seat Reservation with Auto Timeout
# ============================================================================
class SeatUnavailableError(Exception):
    pass


LOCK_DURATION = timedelta(minutes=2)


@transaction.atomic
def lock_seats(show_id, seat_ids, user):
    """
    Pessimistic locking: `select_for_update()` makes a second concurrent
    request physically wait at the DB row lock, so it can never read stale
    "seat is free" data. The partial UniqueConstraint on SeatLock (see
    models.py) is the second safety net in case two locks are attempted
    from different connections before either commits.
    """
    show = Show.objects.select_related("theater").get(id=show_id)
    seats = list(Seat.objects.select_for_update().filter(id__in=seat_ids, theater=show.theater))
    if len(seats) != len(seat_ids):
        raise SeatUnavailableError("One or more seats do not exist for this theater.")

    now = timezone.now()
    conflicts = SeatLock.objects.select_for_update().filter(
        show=show, seat_id__in=seat_ids,
        status__in=[SeatLockStatus.LOCKED, SeatLockStatus.CONFIRMED], expires_at__gt=now,
    )
    if conflicts.exists():
        taken = set(conflicts.values_list("seat_id", flat=True))
        raise SeatUnavailableError(f"Seats already held: {sorted(taken)}")

    created = []
    try:
        for seat in seats:
            created.append(SeatLock.objects.create(
                show=show, seat=seat, user=user,
                status=SeatLockStatus.LOCKED, expires_at=now + LOCK_DURATION,
            ))
    except IntegrityError:
        raise SeatUnavailableError("Seat was just taken by another user.")
    return created


@require_POST
@login_required
def reserve_seats(request, show_id):
    body = json.loads(request.body)
    try:
        locks = lock_seats(show_id, body.get("seat_ids", []), request.user)
    except SeatUnavailableError as exc:
        return JsonResponse({"error": str(exc)}, status=409)
    return JsonResponse({
        "locked_seats": [l.seat_id for l in locks],
        "expires_at": locks[0].expires_at.isoformat() if locks else None,
    })


@transaction.atomic
def confirm_seat_locks(booking):
    """Called after successful payment — flips locked -> confirmed."""
    SeatLock.objects.filter(
        show=booking.show, seat__in=booking.seats.all(), user=booking.user,
        status=SeatLockStatus.LOCKED,
    ).update(status=SeatLockStatus.CONFIRMED)


# ============================================================================
# TASK 4 — Payment Gateway Integration with Idempotency and Webhook Security
# ============================================================================
@require_POST
@login_required
def create_checkout_session(request):
    """
    Idempotency: client sends the same idempotency_key on retry; we never
    create a second Booking/Payment for the same key. Amount is read from
    the server-side Booking record, never trusted from client input.
    """
    body = json.loads(request.body)
    idempotency_key = body.get("idempotency_key")
    booking = Booking.objects.select_related("show__movie").get(
        id=body["booking_id"], user=request.user
    )
    if str(booking.idempotency_key) != idempotency_key:
        return JsonResponse({"error": "idempotency key mismatch"}, status=400)

    existing = Payment.objects.filter(booking=booking).first()
    if existing and existing.status not in ("failed", "cancelled"):
        return JsonResponse({"session_id": existing.provider_payment_id})

    session = stripe.checkout.Session.create(
        idempotency_key=str(booking.idempotency_key),
        payment_method_types=["card"], mode="payment",
        line_items=[{
            "price_data": {
                "currency": "inr",
                "unit_amount": int(booking.total_amount * 100),
                "product_data": {"name": f"{booking.show.movie.title} tickets"},
            },
            "quantity": 1,
        }],
        metadata={"booking_id": str(booking.id)},
        success_url=settings.PAYMENT_SUCCESS_URL + "?session_id={CHECKOUT_SESSION_ID}",
        cancel_url=settings.PAYMENT_CANCEL_URL,
    )
    Payment.objects.update_or_create(
        booking=booking,
        defaults={"provider_payment_id": session.id, "amount": booking.total_amount, "status": "created"},
    )
    return JsonResponse({"session_id": session.id, "checkout_url": session.url})


@csrf_exempt
@require_POST
def stripe_webhook(request):
    """
    Signature verified BEFORE anything in the payload is trusted — an
    unsigned/forged payload gets a 400 and is never processed. Each
    event.id is recorded in WebhookEvent before processing; a duplicate
    delivery (Stripe retries at-least-once) hits the unique constraint and
    is acknowledged as a no-op instead of double-confirming a booking.
    """
    payload = request.body
    sig_header = request.META.get("HTTP_STRIPE_SIGNATURE", "")
    try:
        event = stripe.Webhook.construct_event(payload, sig_header, settings.STRIPE_WEBHOOK_SECRET)
    except (ValueError, stripe.error.SignatureVerificationError) as exc:
        logger.warning("Rejected webhook: bad signature/payload (%s)", exc)
        return HttpResponse(status=400)

    try:
        with transaction.atomic():
            WebhookEvent.objects.create(provider="stripe", event_id=event["id"])
    except IntegrityError:
        return HttpResponse(status=200)  # already processed

    event_type = event["type"]
    data_object = event["data"]["object"]

    if event_type == "checkout.session.completed":
        _handle_payment_success(data_object)
    elif event_type in ("payment_intent.payment_failed", "checkout.session.expired"):
        _handle_payment_failure(data_object)
    else:
        logger.info("Unhandled stripe event type: %s", event_type)
    return HttpResponse(status=200)


@transaction.atomic
def _handle_payment_success(session_obj):
    booking_id = session_obj.get("metadata", {}).get("booking_id")
    if not booking_id:
        logger.error("Webhook success event missing booking_id metadata")
        return
    booking = Booking.objects.select_for_update().select_related("payment").get(id=booking_id)
    if booking.status == Booking.Status.CONFIRMED:
        return

    booking.status = Booking.Status.CONFIRMED
    booking.save(update_fields=["status"])
    Payment.objects.filter(booking=booking).update(status="succeeded")
    confirm_seat_locks(booking)

    # Queued, not inline — webhook responds to Stripe immediately.
    send_booking_confirmation_email.delay(booking.id)


@transaction.atomic
def _handle_payment_failure(session_obj):
    booking_id = session_obj.get("metadata", {}).get("booking_id")
    if not booking_id:
        return
    booking = Booking.objects.select_for_update().get(id=booking_id)
    booking.status = Booking.Status.FAILED
    booking.save(update_fields=["status"])
    Payment.objects.filter(booking=booking).update(status="failed")
    SeatLock.objects.filter(show=booking.show, seat__in=booking.seats.all()).update(
        status=SeatLockStatus.RELEASED
    )


# ============================================================================
# TASK 6 — Advanced Admin Analytics Dashboard with Aggregation Optimization
# ============================================================================
CACHE_TTL_SECONDS = 120


def is_admin(user):
    return user.is_authenticated and (user.is_staff or user.groups.filter(name="Admin").exists())


def _cached(key, compute_fn):
    value = cache.get(key)
    if value is None:
        value = compute_fn()
        cache.set(key, value, CACHE_TTL_SECONDS)
    return value


@login_required
@user_passes_test(is_admin)
def revenue_summary(request):
    def compute():
        confirmed = Payment.objects.filter(status="succeeded")
        now = timezone.now()
        daily = confirmed.filter(created_at__gte=now - timedelta(days=1)).aggregate(t=Sum("amount"))["t"] or 0
        weekly = confirmed.filter(created_at__gte=now - timedelta(days=7)).aggregate(t=Sum("amount"))["t"] or 0
        monthly = confirmed.filter(created_at__gte=now - timedelta(days=30)).aggregate(t=Sum("amount"))["t"] or 0
        daily_series = (
            confirmed.filter(created_at__gte=now - timedelta(days=30))
            .annotate(day=TruncDay("created_at")).values("day")
            .annotate(total=Sum("amount")).order_by("day")
        )
        return {
            "daily_total": float(daily), "weekly_total": float(weekly), "monthly_total": float(monthly),
            "daily_series": [{"day": d["day"].isoformat(), "total": float(d["total"])} for d in daily_series],
        }
    return JsonResponse(_cached("dashboard:revenue_summary", compute))


@login_required
@user_passes_test(is_admin)
def popular_movies(request):
    def compute():
        qs = (
            Booking.objects.filter(status=Booking.Status.CONFIRMED)
            .values("show__movie__id", "show__movie__title")
            .annotate(booking_count=Count("id")).order_by("-booking_count")[:10]
        )
        return {"movies": list(qs)}
    return JsonResponse(_cached("dashboard:popular_movies", compute))


@login_required
@user_passes_test(is_admin)
def busiest_theaters(request):
    def compute():
        qs = (
            Theater.objects.annotate(
                shows_count=Count("shows", distinct=True),
                booked_seats=Count(
                    "shows__bookings__seats",
                    filter=Q(shows__bookings__status=Booking.Status.CONFIRMED), distinct=True,
                ),
            )
            .annotate(capacity=ExpressionWrapper(F("total_seats") * F("shows_count"), output_field=FloatField()))
            .filter(shows_count__gt=0)
            .annotate(occupancy_rate=ExpressionWrapper(F("booked_seats") * 1.0 / F("capacity"), output_field=FloatField()))
            .order_by("-occupancy_rate")
            .values("id", "name", "city", "occupancy_rate")[:10]
        )
        return {"theaters": list(qs)}
    return JsonResponse(_cached("dashboard:busiest_theaters", compute))


@login_required
@user_passes_test(is_admin)
def peak_booking_hours(request):
    def compute():
        qs = (
            Booking.objects.filter(status=Booking.Status.CONFIRMED)
            .annotate(hour=ExtractHour("created_at")).values("hour")
            .annotate(count=Count("id")).order_by("hour")
        )
        return {"hourly_distribution": list(qs)}
    return JsonResponse(_cached("dashboard:peak_hours", compute))


@login_required
@user_passes_test(is_admin)
def cancellation_rate(request):
    def compute():
        totals = Booking.objects.aggregate(
            total=Count("id"), cancelled=Count("id", filter=Q(status=Booking.Status.CANCELLED)),
        )
        total = totals["total"] or 1
        return {"cancellation_rate": totals["cancelled"] / total,
                "total_bookings": totals["total"], "cancelled": totals["cancelled"]}
    return JsonResponse(_cached("dashboard:cancellation_rate", compute))
