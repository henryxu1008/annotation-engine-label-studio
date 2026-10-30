from django.urls import path

from . import field_batch_api as batches
from .field_overview_api import OverviewAPI

urlpatterns = [
    path('<int:batch_id>/quality-overview/', OverviewAPI.as_view()),
    path('capability/', batches.CapabilityAPI.as_view()),
    path('preview/', batches.PreviewAPI.as_view()),
    path('', batches.ListAPI.as_view()),
    path('<int:batch_id>/', batches.DetailAPI.as_view()),
    path('<int:batch_id>/items/', batches.ItemsAPI.as_view()),
    path('<int:batch_id>/items/<int:task_id>/', batches.ItemAPI.as_view()),
]
