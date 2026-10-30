from django.urls import path

from .field_api import CapabilityAPI, ReportAPI, SourcesAPI

urlpatterns = [
    path('capability/', CapabilityAPI.as_view()),
    path('tasks/<int:task_id>/sources/', SourcesAPI.as_view()),
    path('tasks/<int:task_id>/', ReportAPI.as_view()),
]
