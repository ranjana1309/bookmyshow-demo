"""
bookings/urls.py — every endpoint for all six tasks, in one file.

In your project's root urls.py, add ONE line:
    from django.urls import path, include
    urlpatterns = [
        ...,
        path("api/", include("bookings.urls")),
    ]

Every route below is then reachable under /api/... — one include, one
file, no scattered url modules per feature.
"""
from django.urls import path
from . import views

urlpatterns = [
    # Task 1 — Filtering
    path("movies/", views.MovieListView.as_view(), name="movie-list"),

    # Task 3 — Trailer (bundled into movie detail)
    path("movies/<int:movie_id>/", views.movie_detail, name="movie-detail"),

    # Task 5 — Seat reservation
    path("shows/<int:show_id>/reserve-seats/", views.reserve_seats, name="reserve-seats"),

    # Task 4 — Payments
    path("payments/checkout/", views.create_checkout_session, name="create-checkout"),
    path("payments/webhook/stripe/", views.stripe_webhook, name="stripe-webhook"),

    # Task 6 — Admin analytics
    path("admin/analytics/revenue/", views.revenue_summary, name="admin-revenue"),
    path("admin/analytics/popular-movies/", views.popular_movies, name="admin-popular-movies"),
    path("admin/analytics/busiest-theaters/", views.busiest_theaters, name="admin-busiest-theaters"),
    path("admin/analytics/peak-hours/", views.peak_booking_hours, name="admin-peak-hours"),
    path("admin/analytics/cancellation-rate/", views.cancellation_rate, name="admin-cancellation-rate"),
]

# Note: Task 2 (email confirmation) has no URL of its own — it's triggered
# from inside the payment webhook handler (views.py: _handle_payment_success)
# via send_booking_confirmation_email.delay(booking.id), not called directly
# from the frontend.
