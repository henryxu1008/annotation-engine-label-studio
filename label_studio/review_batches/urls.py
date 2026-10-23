from django.urls import path

from .api import CapabilityAPI, DetailAPI, ListAPI, PreviewAPI, QueueAPI, ReworkAPI

urlpatterns = [
    path("capability/", CapabilityAPI.as_view()),
    path("preview/", PreviewAPI.as_view()),
    path("", ListAPI.as_view()),
    path("<int:batch_id>/", DetailAPI.as_view()),
    path("<int:batch_id>/queue/", QueueAPI.as_view()),
    path("<int:batch_id>/rework/<int:source_id>/", ReworkAPI.as_view()),
]
