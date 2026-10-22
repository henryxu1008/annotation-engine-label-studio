from django.urls import path

from .api import PackageCapabilityAPI, PackageDetailAPI, PackageListAPI

urlpatterns = [
    path('capability/', PackageCapabilityAPI.as_view(), name='capability'),
    path('', PackageListAPI.as_view(), name='list'),
    path('<int:package_id>/', PackageDetailAPI.as_view(), name='detail'),
]
