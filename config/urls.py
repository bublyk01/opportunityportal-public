from django.contrib import admin
from django.urls import include, path

from opportunities.views import opportunity_list

admin.site.site_header = "OpportunityHub administration"
admin.site.site_title = "OpportunityHub admin"
admin.site.index_title = "Manage opportunities"

urlpatterns = [
    path("", opportunity_list, name="home"),
    path("opportunities/", include("opportunities.urls")),
    path("admin/", admin.site.urls),
]
