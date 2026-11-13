"""This file and its contents are licensed under the Apache License 2.0. Please see the included NOTICE for copyright information and LICENSE for a copy of the license.
"""
from classification_disagreements.bbox_arbitration_api import AssigneesAPI as BboxArbitrationAssigneesAPI
from classification_disagreements.bbox_arbitration_api import CancelAPI as BboxArbitrationCancelAPI
from classification_disagreements.bbox_arbitration_api import CapabilityAPI as BboxArbitrationCapabilityAPI
from classification_disagreements.bbox_arbitration_api import CreateAPI as BboxArbitrationCreateAPI
from classification_disagreements.bbox_arbitration_api import DecideAPI as BboxArbitrationDecideAPI
from classification_disagreements.bbox_arbitration_api import DetailAPI as BboxArbitrationDetailAPI
from classification_disagreements.bbox_arbitration_api import ImageAPI as BboxArbitrationImageAPI
from classification_disagreements.bbox_batch_api import CapabilityAPI as BboxBatchCapabilityAPI
from classification_disagreements.bbox_batch_api import CreateAPI as BboxBatchCreateAPI
from classification_disagreements.bbox_batch_api import DetailAPI as BboxBatchDetailAPI
from classification_disagreements.bbox_batch_api import ImageAPI as BboxBatchImageAPI
from classification_disagreements.bbox_batch_api import ItemAPI as BboxBatchItemAPI
from classification_disagreements.bbox_batch_api import ItemsAPI as BboxBatchItemsAPI
from classification_disagreements.bbox_batch_api import PreviewAPI as BboxBatchPreviewAPI
from classification_disagreements.bbox_media_api import MediaAPI as BboxMediaAPI
from classification_disagreements.bbox_media_api import PrepareAPI as BboxPrepareAPI
from django.urls import include, path
from review_batches.confidence_api import InputAPI as ConfidenceInputAPI
from review_batches.confidence_api import ObservationAPI as ConfidenceObservationAPI
from tasks.my_work import MyWorkAPI
from classification_disagreements.blind_editor_api import BlindWorkListAPI

from . import api, views

app_name = 'projects'

# reverse for projects:name
_urlpatterns = [
    path('', views.project_list, name='project-index'),
    path('<int:pk>/settings/', views.project_settings, name='project-settings', kwargs={'sub_path': ''}),
    path('<int:pk>/settings/<sub_path>', views.project_settings, name='project-settings-anything'),
]

# reverse for projects:api:name
_api_urlpatterns = [
    # CRUD
    path('', api.ProjectListAPI.as_view(), name='project-list'),
    path('<int:pk>/', api.ProjectAPI.as_view(), name='project-detail'),
    path('<int:pk>/my-work/', MyWorkAPI.as_view(), name='project-my-work'),
    path('<int:pk>/blind-work/', BlindWorkListAPI.as_view(), name='project-blind-work'),
    path('<int:pk>/bbox-media/tasks/<int:task_id>/prepare/', BboxPrepareAPI.as_view(), name='bbox-prepare'),
    path('<int:pk>/bbox-media/<uuid:token>/', BboxMediaAPI.as_view(), name='bbox-media'),
    path('<int:pk>/bbox-quality/preview/', BboxBatchPreviewAPI.as_view(), name='bbox-quality-preview'),
    path('<int:pk>/bbox-quality/capability/', BboxBatchCapabilityAPI.as_view(), name='bbox-quality-capability'),
    path('<int:pk>/bbox-quality/batches/', BboxBatchCreateAPI.as_view(), name='bbox-quality-create'),
    path('<int:pk>/bbox-quality/batches/<int:batch_id>/', BboxBatchDetailAPI.as_view(), name='bbox-quality-detail'),
    path('<int:pk>/bbox-quality/batches/<int:batch_id>/items/', BboxBatchItemsAPI.as_view(), name='bbox-quality-items'),
    path('<int:pk>/bbox-quality/batches/<int:batch_id>/items/<int:task_id>/', BboxBatchItemAPI.as_view(), name='bbox-quality-item'),
    path('<int:pk>/bbox-quality/batches/<int:batch_id>/items/<int:task_id>/image/', BboxBatchImageAPI.as_view(), name='bbox-quality-image'),
    path('<int:pk>/bbox-arbitrations/', BboxArbitrationCreateAPI.as_view(), name='bbox-arbitration-create'),
    path('<int:pk>/bbox-arbitrations/assignees/', BboxArbitrationAssigneesAPI.as_view(), name='bbox-arbitration-assignees'),
    path('<int:pk>/bbox-arbitrations/capability/', BboxArbitrationCapabilityAPI.as_view(), name='bbox-arbitration-capability'),
    path('<int:pk>/bbox-arbitrations/<int:arbitration_id>/', BboxArbitrationDetailAPI.as_view(), name='bbox-arbitration-detail'),
    path('<int:pk>/bbox-arbitrations/<int:arbitration_id>/image/', BboxArbitrationImageAPI.as_view(), name='bbox-arbitration-image'),
    path('<int:pk>/bbox-arbitrations/<int:arbitration_id>/decide/', BboxArbitrationDecideAPI.as_view(), name='bbox-arbitration-decide'),
    path('<int:pk>/bbox-arbitrations/<int:arbitration_id>/cancel/', BboxArbitrationCancelAPI.as_view(), name='bbox-arbitration-cancel'),
    path('<int:pk>/task-packages/', include(('task_packages.urls', 'task_packages'), namespace='task-packages')),
    path('<int:pk>/review-batches/', include(('review_batches.urls', 'review_batches'), namespace='review-batches')),
    path('<int:pk>/second-reviews/', include('review_batches.second_review_api')),
    path('<int:pk>/second-review-followups/', include('review_batches.followup_api')),
    path('<int:pk>/review-confidence-inputs/<int:source_id>/', ConfidenceInputAPI.as_view()),
    path('<int:pk>/review-confidence-observations/', ConfidenceObservationAPI.as_view()),
    path('<int:pk>/classification-disagreements/', include('classification_disagreements.urls')),
    path('<int:pk>/field-disagreements/', include('classification_disagreements.field_urls')),
    path('<int:pk>/ner-disagreements/', include('classification_disagreements.ner_urls')),
    path('<int:pk>/ner-arbitrations/', include('classification_disagreements.ner_arbitration_urls')),
    path('<int:pk>/field-arbitrations/', include('classification_disagreements.field_arbitration_urls')),
    path('<int:pk>/classification-arbitrations/', include('classification_disagreements.arbitration_urls')),
    path('<int:pk>/members/', api.ProjectMemberListCreateAPI.as_view(), name='project-member-list'),
    path(
        '<int:pk>/members/candidates/',
        api.ProjectMemberCandidateListAPI.as_view(),
        name='project-member-candidates',
    ),
    path(
        '<int:pk>/members/capability/',
        api.ProjectMemberCapabilityAPI.as_view(),
        name='project-member-capability',
    ),
    path(
        '<int:pk>/review-capability/',
        api.ProjectReviewCapabilityAPI.as_view(),
        name='project-review-capability',
    ),
    path('<int:pk>/members/<int:member_pk>/', api.ProjectMemberAPI.as_view(), name='project-member-detail'),
    path('counts/', api.ProjectCountsListAPI.as_view(), name='project-counts-list'),
    # Get next task
    path('<int:pk>/next/', api.ProjectNextTaskAPI.as_view(), name='project-next'),
    # Label stream history
    path('<int:pk>/label-stream-history/', api.LabelStreamHistoryAPI.as_view(), name='label-stream-history'),
    # Validate label config in general
    path('validate/', api.LabelConfigValidateAPI.as_view(), name='label-config-validate'),
    # Validate label config for project
    path('<int:pk>/validate/', api.ProjectLabelConfigValidateAPI.as_view(), name='project-label-config-validate'),
    # Project summary
    path('<int:pk>/summary/', api.ProjectSummaryAPI.as_view(), name='project-summary'),
    # Project summary
    path(
        '<int:pk>/summary/reset/',
        api.ProjectSummaryResetAPI.as_view(),
        name='project-summary-reset',
    ),
    # Project import
    path('<int:pk>/imports/<int:import_pk>/', api.ProjectImportAPI.as_view(), name='project-imports'),
    # Project reimport
    path('<int:pk>/reimports/<int:reimport_pk>/', api.ProjectReimportAPI.as_view(), name='project-reimports'),
    # Tasks list for the project: get and destroy
    path('<int:pk>/tasks/', api.ProjectTaskListAPI.as_view(), name='project-tasks-list'),
    # Generate sample task for this project
    path('<int:pk>/sample-task/', api.ProjectSampleTask.as_view(), name='project-sample-task'),
    # List available model versions
    path('<int:pk>/model-versions/', api.ProjectModelVersions.as_view(), name='project-model-versions'),
    # List all annotators for project
    path('<int:pk>/annotators/', api.ProjectAnnotatorsAPI.as_view(), name='project-annotators'),
]

_api_urlpatterns_templates = [
    path('', api.TemplateListAPI.as_view(), name='template-list'),
]


urlpatterns = [
    path('projects/', include(_urlpatterns)),
    path('api/projects/', include((_api_urlpatterns, app_name), namespace='api')),
    path('api/templates/', include((_api_urlpatterns_templates, app_name), namespace='api-templates')),
]
