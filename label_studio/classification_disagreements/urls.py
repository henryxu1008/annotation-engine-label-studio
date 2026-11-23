from django.urls import path

from . import batch_api
from .api import CapabilityAPI, ReportAPI, SourcesAPI
from .partial_api import PartialAPI
from .overview_api import OverviewAPI
from .gold_api import GoldAPI, GoldSourcesAPI
from .blind_legacy_api import BlindLegacyBindingsAPI
from .blind_dispatch_api import BlindMixCommandAPI

urlpatterns = [
    path('gold/mix/', BlindMixCommandAPI.as_view()),
    path('gold/mix/<int:predecessor_id>/reassign/', BlindMixCommandAPI.as_view()),
    path('gold/legacy-bindings/', BlindLegacyBindingsAPI.as_view()),
    path('gold/', GoldAPI.as_view()),
    path('gold/tasks/<int:task_id>/sources/', GoldSourcesAPI.as_view()),
    path('gold/<int:evidence_id>/', GoldAPI.as_view()),
    path('batches/<int:batch_id>/quality-overview/', OverviewAPI.as_view()),
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
