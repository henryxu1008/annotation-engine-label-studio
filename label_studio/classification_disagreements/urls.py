from django.urls import path

from . import batch_api
from .api import CapabilityAPI, ReportAPI, SourcesAPI
from .partial_api import PartialAPI

urlpatterns = [
    path('tasks/<int:task_id>/partial-match/', PartialAPI.as_view()),
    path('batches/<int:batch_id>/items/<int:task_id>/partial-match/', PartialAPI.as_view()),
    path('batches/capability/', batch_api.CapabilityAPI.as_view()),
    path('batches/preview/', batch_api.PreviewAPI.as_view()),
    path('batches/', batch_api.ListAPI.as_view()),
    path('batches/<int:batch_id>/', batch_api.DetailAPI.as_view()),
    path('batches/<int:batch_id>/items/', batch_api.ItemsAPI.as_view()),
    path('batches/<int:batch_id>/items/<int:task_id>/', batch_api.ItemAPI.as_view()),
    path('capability/', CapabilityAPI.as_view()),
    path('tasks/<int:task_id>/sources/', SourcesAPI.as_view()),
    path('tasks/<int:task_id>/', ReportAPI.as_view()),
]
