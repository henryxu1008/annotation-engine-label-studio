from django.urls import path

from . import handoff_api, ner_handoff_api
from .api import CapabilityAPI, DetailAPI, ListAPI, PreviewAPI, QueueAPI, ReworkAPI

urlpatterns = [
    path('<int:batch_id>/sources/<int:source_id>/ner-arbitration-context/', ner_handoff_api.ContextAPI.as_view()),
    path('<int:batch_id>/sources/<int:source_id>/ner-arbitration-handoffs/', ner_handoff_api.ListAPI.as_view()),
    path('<int:batch_id>/ner-arbitration-handoffs/<int:handoff_id>/', ner_handoff_api.DetailAPI.as_view()),
    path("<int:batch_id>/sources/<int:source_id>/arbitration-context/", handoff_api.ContextAPI.as_view()),
    path("<int:batch_id>/sources/<int:source_id>/arbitration-handoffs/", handoff_api.ListAPI.as_view()),
    path("<int:batch_id>/arbitration-handoffs/<int:handoff_id>/", handoff_api.DetailAPI.as_view()),
    path("capability/", CapabilityAPI.as_view()),
    path("preview/", PreviewAPI.as_view()),
    path("", ListAPI.as_view()),
    path("<int:batch_id>/", DetailAPI.as_view()),
    path("<int:batch_id>/queue/", QueueAPI.as_view()),
    path("<int:batch_id>/rework/<int:source_id>/", ReworkAPI.as_view()),
]
