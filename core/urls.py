from django.urls import path
from .views import (
    DashboardView, UploadBatchView, BatchDetailView,
    StartAutomationView, ExportBatchView, ResetBatchView,
    RetryFailedView, BatchStatusAPIView, DeleteBatchView,
)

urlpatterns = [
    path('', DashboardView.as_view(), name='dashboard'),
    path('upload/', UploadBatchView.as_view(), name='upload'),
    path('batch/<int:batch_id>/', BatchDetailView.as_view(), name='batch_detail'),
    path('batch/<int:batch_id>/start/', StartAutomationView.as_view(), name='start_automation'),
    path('batch/<int:batch_id>/reset/', ResetBatchView.as_view(), name='reset_batch'),
    path('batch/<int:batch_id>/export/', ExportBatchView.as_view(), name='export_batch'),
    path('batch/<int:batch_id>/retry/', RetryFailedView.as_view(), name='retry_failed'),
    path('batch/<int:batch_id>/delete/', DeleteBatchView.as_view(), name='delete_batch'),
    path('api/batch/<int:batch_id>/status/', BatchStatusAPIView.as_view(), name='batch_status_api'),
]
