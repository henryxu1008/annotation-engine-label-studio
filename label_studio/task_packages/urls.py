from django.urls import path

from .api import (
    AvailablePoolsAPI,
    PackageCapabilityAPI,
    PackageClaimAPI,
    PackageDetailAPI,
    PackageDispatchAPI,
    PackageListAPI,
    PackagePoolAPI,
)

urlpatterns = [
    path('capability/', PackageCapabilityAPI.as_view(), name='capability'),
    path('claim-pools/', AvailablePoolsAPI.as_view(), name='claim-pools'),
    path('', PackageListAPI.as_view(), name='list'),
    path('<int:package_id>/', PackageDetailAPI.as_view(), name='detail'),
    path('<int:package_id>/dispatch/', PackageDispatchAPI.as_view(), name='dispatch'),
    path('<int:package_id>/pool/', PackagePoolAPI.as_view(), name='pool'),
    path('<int:package_id>/claim/', PackageClaimAPI.as_view(), name='claim'),
]
