from django.urls import path

from . import arbitration_api as api

urlpatterns = [
    path('capability/', api.CapabilityAPI.as_view()),
    path('assignees/', api.AssigneesAPI.as_view()),
    path('', api.ListAPI.as_view()),
    path('<int:arbitration_id>/', api.DetailAPI.as_view()),
    path('<int:arbitration_id>/decision/', api.CommandAPI.as_view()),
    path('<int:arbitration_id>/cancel/', api.CancelAPI.as_view()),
]
