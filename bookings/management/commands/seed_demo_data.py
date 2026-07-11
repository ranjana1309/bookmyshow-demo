"""
Run with: python manage.py seed_demo_data
Creates a handful of genres, languages, movies, a theater, and a show so
the homepage and admin dashboard have something to display immediately.
"""
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from bookings.models import Genre, Language, Movie, Theater, Show, Seat


class Command(BaseCommand):
    help = "Seed the database with demo movies/genres/languages/theater/show/seats"

    def handle(self, *args, **options):
        action, comedy, drama = (
            Genre.objects.get_or_create(name=n)[0] for n in ["Action", "Comedy", "Drama"]
        )
        english, hindi = (Language.objects.get_or_create(name=n)[0] for n in ["English", "Hindi"])

        m1 = Movie.objects.get_or_create(
            title="Skyline Protocol",
            defaults={"release_date": timezone.now().date(), "trailer_url": "https://youtu.be/dQw4w9WgXcQ"},
        )[0]
        m1.genres.set([action])
        m1.languages.set([english])

        m2 = Movie.objects.get_or_create(
            title="Chai Pe Comedy",
            defaults={"release_date": timezone.now().date() - timedelta(days=10)},
        )[0]
        m2.genres.set([comedy, drama])
        m2.languages.set([hindi])

        theater = Theater.objects.get_or_create(
            name="PVR Central", city="Bhopal", defaults={"total_seats": 40}
        )[0]

        show = Show.objects.get_or_create(
            movie=m1, theater=theater, language=english,
            defaults={"start_time": timezone.now() + timedelta(hours=3), "price": 250},
        )[0]

        for row in "ABCD":
            for num in range(1, 11):
                Seat.objects.get_or_create(theater=theater, seat_number=f"{row}{num}")

        self.stdout.write(self.style.SUCCESS(
            f"Seeded: {Movie.objects.count()} movies, {Theater.objects.count()} theater(s), "
            f"{Show.objects.count()} show(s), {Seat.objects.count()} seats."
        ))
