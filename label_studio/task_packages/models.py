"""Fixed project-local membership, never an assignment or content snapshot."""

from django.conf import settings
from django.db import models


class TaskPackage(models.Model):
    project = models.ForeignKey('projects.Project', on_delete=models.CASCADE, related_name='task_packages')
    name = models.CharField(max_length=160)
    instructions = models.TextField(blank=True)
    source_note = models.CharField(max_length=500, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)
    creator_principal = models.CharField(max_length=512)
    idempotency_key = models.UUIDField()
    request_hash = models.CharField(max_length=64)
    membership_hash = models.CharField(max_length=64)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['project', 'idempotency_key'], name='package_project_key_unique')
        ]


class TaskPackageItem(models.Model):
    package = models.ForeignKey(TaskPackage, on_delete=models.CASCADE, related_name='items')
    task = models.ForeignKey('tasks.Task', null=True, on_delete=models.SET_NULL, related_name='package_items')
    # Retain membership after deletion; the live FK must match both ID and creation time.
    original_task_id = models.PositiveBigIntegerField()
    original_created_at = models.DateTimeField()

    class Meta:
        constraints = [models.UniqueConstraint(fields=['package', 'original_task_id'], name='package_task_unique')]
