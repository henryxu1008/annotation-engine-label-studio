from django.urls import path

from .api import (
    AvailablePoolsAPI,
    PackageBalancedDispatchAPI,
    PackageBatchAPI,
    PackageCapabilityAPI,
    PackageClaimAPI,
    PackageDetailAPI,
    PackageDispatchAPI,
    PackageListAPI,
    PackageOverdueNoticeAPI,
    PackagePoolAPI,
    PackageReopenAPI,
)

urlpatterns = [
    path('batches/', PackageBatchAPI.as_view(), name='batches'),
    path('capability/', PackageCapabilityAPI.as_view(), name='capability'),
    path('claim-pools/', AvailablePoolsAPI.as_view(), name='claim-pools'),
    path('overdue-notices/', PackageOverdueNoticeAPI.as_view(), name='overdue-notices'),
    path('', PackageListAPI.as_view(), name='list'),
    path('<int:package_id>/', PackageDetailAPI.as_view(), name='detail'),
    path('<int:package_id>/dispatch/', PackageDispatchAPI.as_view(), name='dispatch'),
    path('<int:package_id>/pool/', PackagePoolAPI.as_view(), name='pool'),
    path('<int:package_id>/claim/', PackageClaimAPI.as_view(), name='claim'),
    path('<int:package_id>/reopen/', PackageReopenAPI.as_view(), name='reopen'),
    path('<int:package_id>/balance/', PackageBalancedDispatchAPI.as_view(), name='balance'),
]
