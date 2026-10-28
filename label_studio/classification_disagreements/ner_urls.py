from django.urls import path

from . import ner_batch_api as batches
from .ner_api import CapabilityAPI, ReportAPI, SourcesAPI
from .ner_overview_api import OverviewAPI

urlpatterns = [
    path('batches/capability/', batches.CapabilityAPI.as_view()),
    path('batches/preview/', batches.PreviewAPI.as_view()),
    path('batches/', batches.ListAPI.as_view()),
    path('batches/<int:batch_id>/', batches.DetailAPI.as_view()),
    path('batches/<int:batch_id>/quality-overview/', OverviewAPI.as_view()),
    path('batches/<int:batch_id>/items/', batches.ItemsAPI.as_view()),
    path('batches/<int:batch_id>/items/<int:task_id>/', batches.ItemAPI.as_view()),
    path('capability/', CapabilityAPI.as_view()),
    path('tasks/<int:task_id>/sources/', SourcesAPI.as_view()),
    path('tasks/<int:task_id>/', ReportAPI.as_view()),
]
