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


class TaskPackageDispatch(models.Model):
    """Receipt of a committed request, not a live assignment projection."""

    project = models.ForeignKey('projects.Project', on_delete=models.CASCADE)
    package = models.ForeignKey(TaskPackage, on_delete=models.CASCADE, related_name='dispatches')
    assignee = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name='+')
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name='+')
    assignee_id_snapshot = models.PositiveBigIntegerField()
    actor_id_snapshot = models.PositiveBigIntegerField()
    creator_principal = models.CharField(max_length=512)
    idempotency_key = models.UUIDField()
    request_hash = models.CharField(max_length=64)
    assignment_ids = models.JSONField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['project', 'idempotency_key'], name='package_dispatch_key_unique')
        ]


class TaskPackagePool(models.Model):
    package = models.OneToOneField(TaskPackage, on_delete=models.CASCADE, related_name='pool')
    candidates = models.ManyToManyField(settings.AUTH_USER_MODEL, related_name='+')
    enabled = models.BooleanField(default=True)
    due_at = models.DateTimeField(null=True, blank=True)
    revision = models.PositiveIntegerField(default=1)
    updated_at = models.DateTimeField(auto_now=True)


class TaskPackageClaim(models.Model):
    pool = models.OneToOneField(TaskPackagePool, on_delete=models.CASCADE, related_name='claim')
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name='+')
    actor_id_snapshot = models.PositiveBigIntegerField()
    principal = models.CharField(max_length=512)
    idempotency_key = models.UUIDField()
    assignment_ids = models.JSONField()
    created_at = models.DateTimeField(auto_now_add=True)
