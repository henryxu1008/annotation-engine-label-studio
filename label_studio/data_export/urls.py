"""This file and its contents are licensed under the Apache License 2.0. Please see the included NOTICE for copyright information and LICENSE for a copy of the license.
"""
from django.urls import include, path

from . import api
from .approved_api import ApprovedExportAPI
from .snapshot_api import ReleaseSnapshotAPI

app_name = 'data_export'


_api_urlpatterns = [
    path('<int:pk>/approved-dataset-releases/<uuid:release_id>/snapshots/', ReleaseSnapshotAPI.as_view()),
    *[path(f'<int:pk>/approved-dataset-releases/<uuid:release_id>/snapshots/<uuid:snapshot_id>/{suffix}',
           ReleaseSnapshotAPI.as_view(), {'action': action}) for suffix, action in
      (('', 'detail'), ('manifest/', 'manifest'), ('diff/', 'diff'), ('retry/', 'retry'),
       ('assets/<uuid:asset_id>/download/', 'download'))],
    path('<int:pk>/approved-dataset-releases/', ApprovedExportAPI.as_view()),
    path('<int:pk>/approved-dataset-releases/<uuid:release_id>/', ApprovedExportAPI.as_view(), {'action': 'detail'}),
    path('<int:pk>/approved-dataset-releases/<uuid:release_id>/artifacts/<uuid:artifact_id>/download/',
         ApprovedExportAPI.as_view(), {'action': 'download'}),
    *[path(f'<int:pk>/approved-dataset-releases/{action}/', ApprovedExportAPI.as_view(), {'action': action})
      for action in ('capabilities', 'candidates', 'preview')],
    *[path(f'<int:pk>/approved-dataset-releases/<uuid:release_id>/{action}/', ApprovedExportAPI.as_view(), {'action': action})
      for action in ('items', 'manifest', 'retry')],
    # export api
    path('<int:pk>/export', api.ExportAPI.as_view(), name='project-export'),
    path('<int:pk>/export/formats', api.ExportFormatsListAPI.as_view(), name='project-export-formats'),
    # Previously exported results
    path('<int:pk>/export/files', api.ProjectExportFiles.as_view(), name='project-export-files'),
    path('<int:pk>/exports/', api.ExportListAPI.as_view(), name='project-exports-list'),
    path('<int:pk>/exports/<int:export_pk>', api.ExportDetailAPI.as_view(), name='project-exports-detail'),
    path(
        '<int:pk>/exports/<int:export_pk>/download', api.ExportDownloadAPI.as_view(), name='project-exports-download'
    ),
    path('<int:pk>/exports/<int:export_pk>/convert', api.ExportConvertAPI.as_view(), name='project-exports-convert'),
]

urlpatterns = [
    path('api/projects/', include((_api_urlpatterns, app_name), namespace='api-projects')),
    path('api/auth/export/', api.ProjectExportFilesAuthCheck.as_view(), name='project-export-files-auth-check'),
    # path('api/auth/exports/', api.ExportListAPI.as_view(), name='api-exports'),
]
