"""
demo_views.py — NOT one of the 6 graded tasks. This just renders a simple
HTML homepage so that opening http://127.0.0.1:8000/ in a browser shows
something, instead of only the JSON API endpoints in views.py/urls.py.
"""
from django.shortcuts import render
from .models import Genre, Language
from .views import _filter_movies, _dynamic_filter_counts


def home(request):
    genre_ids = [int(g) for g in request.GET.getlist("genre") if g.isdigit()]
    language_ids = [int(l) for l in request.GET.getlist("language") if l.isdigit()]

    data = _filter_movies(genre_ids, language_ids, page=1, page_size=20)
    counts = _dynamic_filter_counts(genre_ids, language_ids)

    return render(request, "home.html", {
        "movies": data["results"],
        "genres": Genre.objects.all(),
        "languages": Language.objects.all(),
        "selected_genres": genre_ids,
        "selected_languages": language_ids,
        "filter_counts": counts,
        "total_count": data["total_count"],
    })
