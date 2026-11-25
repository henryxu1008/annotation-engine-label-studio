"""This file and its contents are licensed under the Apache License 2.0. Please see the included NOTICE for copyright information and LICENSE for a copy of the license.
"""
from classification_disagreements.blind_editor_api import BlindEditorReadAPI
from classification_disagreements.blind_editor_writes import (
    BlindAnnotationCreateAPI,
    BlindAnnotationUpdateAPI,
    BlindDraftCreateAPI,
    BlindDraftUpdateAPI,
)
from django.urls import include, path
from rest_framework import routers

from . import api
from . import legacy_blind_views as legacy

app_name = 'tasks'

router = routers.DefaultRouter()
router.register(r'predictions', api.PredictionAPI, basename='prediction')

_api_urlpatterns = [
    # CRUD
    path('', api.TaskListAPI.as_view(), name='task-list'),
    path('blind/<int:pk>/', BlindEditorReadAPI.as_view(), name='blind-editor-read'),
    path('blind/<int:task_handle>/annotations/', BlindAnnotationCreateAPI.as_view(), name='blind-annotation-create'),
    path('blind/<int:task_handle>/annotations/<int:resource_handle>/', BlindAnnotationUpdateAPI.as_view(),
         name='blind-annotation-update'),
    path('blind/<int:task_handle>/drafts', BlindDraftCreateAPI.as_view(), name='blind-draft-create'),
    path('blind/<int:task_handle>/annotations/<int:annotation_handle>/drafts', BlindDraftCreateAPI.as_view(),
         name='blind-annotation-draft-create'),
    path('blind/<int:task_handle>/drafts/<int:resource_handle>/', BlindDraftUpdateAPI.as_view(),
         name='blind-draft-update'),
    path('<int:pk>/', legacy.LegacyTaskAPI.as_view(), name='task-detail'),
    path('<int:pk>/annotations/', legacy.LegacyAnnotationsListAPI.as_view(), name='task-annotations'),
    path('<int:pk>/drafts', legacy.LegacyDraftListAPI.as_view(), name='task-drafts'),
    path(
        '<int:pk>/annotations/<int:annotation_id>/drafts',
        legacy.LegacyDraftListAPI.as_view(),
        name='task-annotations-drafts',
    ),
    # Agreement endpoint for Summary view
    path('<int:pk>/agreement/', legacy.LegacyTaskAgreementAPI.as_view(), name='task-agreement'),
]

_api_assignments_urlpatterns = [
    path('eligible-assignees/', api.TaskAssignmentEligibleAssigneeListAPI.as_view(), name='assignment-eligible-assignees'),
    path('', api.TaskAssignmentListCreateAPI.as_view(), name='assignment-list'),
    path('<int:pk>/', api.TaskAssignmentAPI.as_view(), name='assignment-detail'),
]

_api_submissions_urlpatterns = [
    path('', api.SubmissionListAPI.as_view(), name='submission-list'),
    path('<int:pk>/', api.SubmissionAPI.as_view(), name='submission-detail'),
    path('<int:pk>/review/', api.SubmissionReviewAPI.as_view(), name='submission-review'),
    path('<int:pk>/release/', api.SubmissionReleaseAPI.as_view(), name='submission-release'),
]

_api_annotations_urlpatterns = [
    path('<int:pk>/', legacy.LegacyAnnotationAPI.as_view(), name='annotation-detail'),
    path('<int:pk>/convert-to-draft', legacy.LegacyAnnotationConvertAPI.as_view(), name='annotation-convert-to-draft'),
]

_api_drafts_urlpatterns = [
    path('<int:pk>/', legacy.LegacyDraftAPI.as_view(), name='draft-detail'),
]

_api_predictions_urlpatterns = router.urls


urlpatterns = [
    path('api/tasks/', include((_api_urlpatterns, app_name), namespace='api')),
    # TODO: these should be moved to the separate apps
    path('api/annotations/', include((_api_annotations_urlpatterns, app_name), namespace='api-annotations')),
    path('api/drafts/', include((_api_drafts_urlpatterns, app_name), namespace='api-drafts')),
    path('api/task-assignments/', include((_api_assignments_urlpatterns, app_name), namespace='api-assignments')),
    path('api/submissions/', include((_api_submissions_urlpatterns, app_name), namespace='api-submissions')),
    path('api/', include((_api_predictions_urlpatterns, app_name), namespace='api-predictions')),
]
