from django.urls import path

from .api import CapabilityAPI, DetailAPI, ListAPI, PreviewAPI, QueueAPI

urlpatterns = [
    path("capability/", CapabilityAPI.as_view()),
    path("preview/", PreviewAPI.as_view()),
    path("", ListAPI.as_view()),
    path("<int:batch_id>/", DetailAPI.as_view()),
    path("<int:batch_id>/queue/", QueueAPI.as_view()),
]
