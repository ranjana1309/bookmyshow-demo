from django.contrib import admin
from django.urls import path, include
from bookings.demo_views import home

urlpatterns = [
    path("", home, name="home"),                 # <- open http://127.0.0.1:8000/ for this
    path("admin/", admin.site.urls),              # <- Django's built-in admin panel
    path("api/", include("bookings.urls")),       # <- all 6 tasks' JSON endpoints
]
