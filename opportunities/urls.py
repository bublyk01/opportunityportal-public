from django.urls import path

from . import views


app_name = "opportunities"

urlpatterns = [
    path("", views.opportunity_list, name="list"),
    path("<int:pk>/", views.opportunity_detail, name="detail"),
]
