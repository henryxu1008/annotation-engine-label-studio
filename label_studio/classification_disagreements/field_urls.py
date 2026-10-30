from django.urls import include, path

from .field_api import CapabilityAPI, ReportAPI, SourcesAPI

urlpatterns = [
    path('batches/', include('classification_disagreements.field_batch_urls')),
    path('capability/', CapabilityAPI.as_view()),
    path('tasks/<int:task_id>/sources/', SourcesAPI.as_view()),
    path('tasks/<int:task_id>/', ReportAPI.as_view()),
]
