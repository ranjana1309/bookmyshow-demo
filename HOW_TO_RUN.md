# How to actually run this

This is a real, complete Django project (not just app code this time) —
it has a `manage.py`, so it runs standalone with zero setup beyond Python.

## 1. Install Python dependencies
```
pip install -r requirements.txt
```
(If `pip` complains about "externally managed environment", add
`--break-system-packages`, or better, use a virtualenv:
`python -m venv venv && source venv/bin/activate` first.)

## 2. Create the database tables
```
python manage.py migrate
```
This creates `db.sqlite3` in this folder — no separate database server
needed for the demo.

## 3. Add some sample data (optional but recommended)
```
python manage.py seed_demo_data
```
Creates 2 movies, genres, languages, a theater, a show, and 40 seats so
the homepage isn't empty.

## 4. (optional) create an admin login, to see the dashboard endpoints
```
python manage.py createsuperuser
```

## 5. Run the server
```
python manage.py runserver
```

## 6. Open it in your browser
Go to **http://127.0.0.1:8000/**

That's "opening the app" — Django isn't a desktop program with an icon;
it's a web server. `runserver` starts it, and your browser is the client
that connects to it, same as any website.

- `/` — homepage with working genre/language filters (Task 1)
- `/admin/` — Django's built-in admin panel (log in with the superuser
  you created)
- `/api/movies/<id>/` — trailer/detail JSON (Task 3)
- `/api/admin/analytics/...` — dashboard endpoints (Task 6) — log in as
  the superuser at `/admin/` first, then visit these in the same browser
  tab so the session cookie is sent
- `/api/payments/checkout/` and `/api/shows/<id>/reserve-seats/` — these
  need a logged-in user and an actual Booking row to test properly; see
  `bookings/views.py` for what each expects in the request body

## What's simplified for this demo vs. the "real" production setup

To make this runnable with nothing installed except Python packages, this
demo project uses:
- **SQLite** instead of Postgres/MySQL
- **In-memory cache** instead of Redis
- **Console email backend** (prints emails to the terminal) instead of
  real SMTP
- **Celery in "eager" mode** (tasks run instantly, in-process) instead of
  a real Celery worker + Redis broker

None of the *logic* changes — the same filtering, locking, webhook, and
aggregation code runs either way. Swap the settings marked "PRODUCTION" in
`config/settings.py` for the real versions in the original
`bookings_app/settings_additions.py` once you're merging this into your
actual deployed project with a real database and Redis instance.
