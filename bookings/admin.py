from django.contrib import admin
from .models import (
    Genre, Language, Movie, Theater, Show, Seat, SeatLock,
    Booking, Payment, WebhookEvent, EmailDeliveryLog,
)

admin.site.register(Genre)
admin.site.register(Language)
admin.site.register(Movie)
admin.site.register(Theater)
admin.site.register(Show)
admin.site.register(Seat)
admin.site.register(SeatLock)
admin.site.register(Booking)
admin.site.register(Payment)
admin.site.register(WebhookEvent)
admin.site.register(EmailDeliveryLog)
