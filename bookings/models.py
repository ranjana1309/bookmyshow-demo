"""
models.py
=========
Drop these into your existing `movies` / `bookings` app (merge with what you
already have — don't create a second parallel app). Field names match the
common BookMyShow-clone schema; rename to match your existing models if they
differ, the logic that matters is the indexes, constraints and locking.
"""
import uuid
from django.db import models
from django.conf import settings
from django.utils import timezone
from datetime import timedelta


# ----------------------------------------------------------------------
# TASK 1: Genre / Language filtering — indexing strategy
# ----------------------------------------------------------------------
class Genre(models.Model):
    name = models.CharField(max_length=50, unique=True, db_index=True)

    def __str__(self):
        return self.name


class Language(models.Model):
    name = models.CharField(max_length=50, unique=True, db_index=True)

    def __str__(self):
        return self.name


class Movie(models.Model):
    title = models.CharField(max_length=255, db_index=True)
    # ManyToMany because a movie can have multiple genres/languages —
    # this is what makes "multi-select filter" meaningful. `through=` gives
    # us an explicit join table we control the indexing on (see MovieGenre
    # below) instead of relying on Django's auto-generated join table,
    # which doesn't reliably index the reverse (genre_id) column across all
    # DB backends — that reverse index is what "movies in Genre X" needs to
    # avoid a join-table scan.
    genres = models.ManyToManyField(Genre, through="MovieGenre", related_name="movies")
    languages = models.ManyToManyField(Language, related_name="movies")
    release_date = models.DateField(db_index=True)
    is_active = models.BooleanField(default=True, db_index=True)
    poster_url = models.URLField(blank=True)
    trailer_url = models.URLField(blank=True)  # validated in trailer_utils.py
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            # Composite index: most list queries filter on is_active first,
            # then sort by release_date — this index serves both in one pass
            # instead of Django doing a filter scan + separate sort.
            models.Index(fields=["is_active", "-release_date"], name="movie_active_release_idx"),
        ]

    def __str__(self):
        return self.title


# Explicit through-model for the Movie<->Genre M2M (see `through=` above).
# This is the key to task 1's "prevent full table scans" requirement:
# without an indexed reverse column, "movies in Genre X" queries have to
# scan the whole join table instead of doing an indexed lookup.
class MovieGenre(models.Model):
    movie = models.ForeignKey(Movie, on_delete=models.CASCADE)
    genre = models.ForeignKey(Genre, on_delete=models.CASCADE)

    class Meta:
        unique_together = ("movie", "genre")
        indexes = [
            models.Index(fields=["genre", "movie"], name="moviegenre_genre_idx"),
        ]


class Theater(models.Model):
    name = models.CharField(max_length=255)
    city = models.CharField(max_length=100, db_index=True)
    total_seats = models.PositiveIntegerField()


class Show(models.Model):
    movie = models.ForeignKey(Movie, on_delete=models.CASCADE, related_name="shows")
    theater = models.ForeignKey(Theater, on_delete=models.CASCADE, related_name="shows")
    language = models.ForeignKey(Language, on_delete=models.PROTECT)
    start_time = models.DateTimeField(db_index=True)
    price = models.DecimalField(max_digits=8, decimal_places=2)

    class Meta:
        indexes = [
            models.Index(fields=["movie", "start_time"], name="show_movie_time_idx"),
            models.Index(fields=["theater", "start_time"], name="show_theater_time_idx"),
        ]


# ----------------------------------------------------------------------
# TASK 5: Concurrency-safe seat reservation
# ----------------------------------------------------------------------
class Seat(models.Model):
    theater = models.ForeignKey(Theater, on_delete=models.CASCADE, related_name="seats")
    seat_number = models.CharField(max_length=10)  # e.g. "A12"

    class Meta:
        unique_together = ("theater", "seat_number")


class SeatLockStatus(models.TextChoices):
    LOCKED = "locked", "Locked"
    CONFIRMED = "confirmed", "Confirmed"
    RELEASED = "released", "Released"


class SeatLock(models.Model):
    """
    One row per (show, seat) *attempt*. The DB-level uniqueness constraint on
    (show, seat) for rows in an "active" state is what actually prevents
    double-booking — not application logic, since application logic can
    always race. See seat_lock.py for the transaction that uses this.
    """
    show = models.ForeignKey(Show, on_delete=models.CASCADE, related_name="seat_locks")
    seat = models.ForeignKey(Seat, on_delete=models.CASCADE)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    status = models.CharField(max_length=10, choices=SeatLockStatus.choices, default=SeatLockStatus.LOCKED)
    locked_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()

    class Meta:
        constraints = [
            # Partial unique index: only ONE active lock (locked/confirmed)
            # per (show, seat) can exist at a time. Released/expired rows
            # don't count, so the same seat can be relocked later.
            models.UniqueConstraint(
                fields=["show", "seat"],
                condition=models.Q(status__in=["locked", "confirmed"]),
                name="unique_active_seat_lock",
            ),
        ]
        indexes = [
            models.Index(fields=["status", "expires_at"], name="seatlock_expiry_idx"),
        ]

    def save(self, *args, **kwargs):
        if not self.expires_at:
            self.expires_at = timezone.now() + timedelta(minutes=2)
        super().save(*args, **kwargs)


# ----------------------------------------------------------------------
# TASK 4: Payment gateway integration — idempotency
# ----------------------------------------------------------------------
class Booking(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        CONFIRMED = "confirmed", "Confirmed"
        CANCELLED = "cancelled", "Cancelled"
        FAILED = "failed", "Failed"

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="bookings")
    show = models.ForeignKey(Show, on_delete=models.CASCADE, related_name="bookings")
    seats = models.ManyToManyField(Seat)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING, db_index=True)
    # Client generates this once per checkout attempt and resends it on
    # retry — this is the idempotency key that prevents a double-click or
    # a retried request from creating two bookings.
    idempotency_key = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    total_amount = models.DecimalField(max_digits=10, decimal_places=2)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)


class Payment(models.Model):
    booking = models.OneToOneField(Booking, on_delete=models.CASCADE, related_name="payment")
    provider = models.CharField(max_length=30, default="stripe")
    provider_payment_id = models.CharField(max_length=255, blank=True, db_index=True)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    status = models.CharField(max_length=20, default="created", db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)


class WebhookEvent(models.Model):
    """
    Every processed webhook event ID is stored here. Before acting on an
    incoming webhook, we check this table first — this is what makes
    duplicate/replayed webhook deliveries a no-op instead of a double
    booking confirmation or double refund.
    """
    provider = models.CharField(max_length=30)
    event_id = models.CharField(max_length=255)
    received_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("provider", "event_id")


# ----------------------------------------------------------------------
# TASK 2: Email delivery log (for retry + monitoring)
# ----------------------------------------------------------------------
class EmailDeliveryLog(models.Model):
    booking = models.ForeignKey(Booking, on_delete=models.CASCADE, related_name="email_logs")
    status = models.CharField(max_length=20, default="pending")  # pending/sent/failed
    attempts = models.PositiveIntegerField(default=0)
    last_error = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
