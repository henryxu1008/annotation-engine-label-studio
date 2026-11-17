import json
import logging
import tempfile
import time
import traceback
from typing import Callable, Optional

from access_control.authorization import authorization
from access_control.project_operations import MANAGE_TASKS, local_principal, lock_project_operation
from core.feature_flags import flag_set
from core.utils.common import load_func
from data_import.uploader import load_tasks_for_async_import_streaming
from django.apps import apps
from django.conf import settings
from django.db import transaction
from label_studio_sdk.label_interface import LabelInterface
from projects.models import ProjectImport, ProjectReimport, ProjectSummary
from rest_framework.exceptions import ValidationError
from tasks.models import Task
from tasks.serializers import sanitize_prediction_import_payload
from users.models import User
from webhooks.models import WebhookAction
from webhooks.utils import emit_webhooks_for_instance

from .models import FileUpload
from .serializers import ImportApiSerializer
from .uploader import load_tasks_for_async_import

logger = logging.getLogger(__name__)


def async_import_background(
    import_id, user_id, recalculate_stats_func: Optional[Callable[..., None]] = None, actor_id=None, **kwargs
):
    with transaction.atomic():
        try:
            project_import = ProjectImport.objects.select_for_update().get(id=import_id)
        except ProjectImport.DoesNotExist:
            logger.error(f'ProjectImport with id {import_id} not found, import processing failed')
            return
        if project_import.status != ProjectImport.Status.CREATED:
            logger.error(f'Processing import with id {import_id} already started')
            return
        project_import.status = ProjectImport.Status.IN_PROGRESS
        project_import.save(update_fields=['status'])

    user = User.objects.get(id=user_id)
    actor = User.objects.get(id=actor_id) if actor_id is not None else user

    if flag_set('fflag_fix_back_plt_902_async_import_background_oom_fix_22092025_short', user='auto'):
        logger.info(f'Using streaming import for project {project_import.project.id}')
        _async_import_background_streaming(project_import, user, actor)
        return

    start = time.time()
    project = project_import.project
    source_config = project.label_config
    tasks = None
    # upload files from request, and parse all tasks
    # TODO: Stop passing request to load_tasks function, make all validation before
    tasks, file_upload_ids, found_formats, data_columns = load_tasks_for_async_import(project_import, user)

    if project_import.preannotated_from_fields:
        # turn flat task JSONs {"column1": value, "column2": value} into {"data": {"column1"..}, "predictions": [{..."column2"}]
        raise_errors = flag_set(
            'fflag_feat_utc_210_prediction_validation_15082025', user=project.organization.created_by
        )
        logger.info(f'Reformatting predictions with raise_errors: {raise_errors}')
        tasks = reformat_predictions(tasks, project_import.preannotated_from_fields, project, raise_errors)

    # Always validate predictions regardless of commit_to_project setting
    if project.label_config_is_not_default and flag_set(
        'fflag_feat_utc_210_prediction_validation_15082025', user=project.organization.created_by
    ):
        validation_errors = []
        li = LabelInterface(project.label_config)

        for i, task in enumerate(tasks):
            if 'predictions' in task:
                for j, prediction in enumerate(task['predictions']):
                    try:
                        prediction = sanitize_prediction_import_payload(prediction)
                        validation_errors_list = li.validate_prediction(prediction, return_errors=True)
                        if validation_errors_list:
                            for error in validation_errors_list:
                                validation_errors.append(f'Task {i}, prediction {j}: {error}')
                    except Exception as e:
                        error_msg = f'Task {i}, prediction {j}: Error validating prediction - {str(e)}'
                        validation_errors.append(error_msg)
                        logger.error(f'Exception during validation: {error_msg}')

        if validation_errors:
            error_message = f'Prediction validation failed ({len(validation_errors)} errors):\n'
            for error in validation_errors:
                error_message += f'- {error}\n'

            if flag_set('fflag_feat_utc_210_prediction_validation_15082025', user=project.organization.created_by):
                project_import.error = error_message
                project_import.status = ProjectImport.Status.FAILED
                project_import.save(update_fields=['error', 'status'])
                return
            else:
                logger.error(
                    f'Prediction validation failed, not raising error - ({len(validation_errors)} errors):\n{error_message}'
                )

    if project_import.commit_to_project:
        try:
            with transaction.atomic():
                principal = local_principal(actor)
                project, actor = lock_project_operation(project.pk, principal, MANAGE_TASKS)
                authorization.require(
                    authorization.can_manage_project(principal, project),
                    'Project manager role is required to import tasks.',
                )
                if project.label_config != source_config:
                    raise ValidationError('Project label configuration changed during import; retry.')
                # Lock summary for update to avoid race conditions
                summary = ProjectSummary.objects.select_for_update().get(project=project)

                # Immediately create project tasks and update project states and counters
                serializer = ImportApiSerializer(data=tasks, many=True, context={'project': project, 'user': actor})
                serializer.is_valid(raise_exception=True)
                tasks = serializer.save(project_id=project.id)

                task_count = len(tasks)
                annotation_count = len(serializer.db_annotations)
                prediction_count = len(serializer.db_predictions)
                # Update counters (like total_annotations) for new tasks and after bulk update tasks stats. It should be a
                # single operation as counters affect bulk is_labeled update

                recalculate_stats_counts = {
                    'task_count': task_count,
                    'annotation_count': annotation_count,
                    'prediction_count': prediction_count,
                }

                project.update_tasks_counters_and_task_states(
                    tasks_queryset=tasks,
                    maximum_annotations_changed=False,
                    overlap_cohort_percentage_changed=False,
                    tasks_number_changed=True,
                    recalculate_stats_counts=recalculate_stats_counts,
                )
                logger.info('Tasks bulk_update finished (async import)')

                summary.update_data_columns(tasks)
                # TODO: summary.update_created_annotations_and_labels
                created_task_ids = [task.pk for task in tasks]
                project_import.task_count = task_count
                project_import.annotation_count = annotation_count
                project_import.prediction_count = prediction_count
                project_import.duration = time.time() - start
                project_import.file_upload_ids = file_upload_ids
                project_import.found_formats = found_formats
                project_import.data_columns = list(data_columns)
                if project_import.return_task_ids:
                    project_import.task_ids = created_task_ids
                project_import.status = ProjectImport.Status.COMPLETED
                project_import.save()
                if getattr(settings, 'ANNOTATION_STORAGE_ADMISSION_ENABLED', False):
                    from webhooks.durable import queue_task_created_webhooks

                    queue_task_created_webhooks(project, tasks)
                else:
                    transaction.on_commit(
                        lambda: emit_webhooks_for_instance(
                            project.organization, project, WebhookAction.TASKS_CREATED, created_task_ids
                        ),
                        robust=True,
                    )
            return
        except Exception as e:
            # Catch outside the atomic block: task writes and counters must roll back
            # before the failure receipt is persisted.
            project_import.error = f'Error creating tasks: {str(e)}'
            project_import.status = ProjectImport.Status.FAILED
            project_import.save(update_fields=['error', 'status'])
            return
    else:
        # Do nothing - just output file upload ids for further use
        task_count = len(tasks)
        annotation_count = None
        prediction_count = None

    duration = time.time() - start

    project_import.task_count = task_count or 0
    project_import.annotation_count = annotation_count or 0
    project_import.prediction_count = prediction_count or 0
    project_import.duration = duration
    project_import.file_upload_ids = file_upload_ids
    project_import.found_formats = found_formats
    project_import.data_columns = data_columns
    if project_import.return_task_ids:
        project_import.task_ids = [task.id for task in tasks]

    project_import.status = ProjectImport.Status.COMPLETED
    project_import.save()


def set_import_background_failure(job, connection, type, value, _):
    import_id = job.args[0]
    ProjectImport.objects.filter(id=import_id).update(
        status=ProjectImport.Status.FAILED, traceback=traceback.format_exc(), error=str(value)
    )


def set_reimport_background_failure(job, connection, type, value, _):
    reimport_id = job.args[0]
    ProjectReimport.objects.filter(id=reimport_id).update(
        status=ProjectReimport.Status.FAILED,
        traceback=traceback.format_exc(),
        error=str(value),
    )


def reformat_predictions(tasks, preannotated_from_fields, project=None, raise_errors=False):
    """
    Transform flat task JSON objects into proper format with separate data and predictions fields.
    Also validates the predictions to ensure they are properly formatted using LabelInterface.

    Args:
        tasks: List of task data
        preannotated_from_fields: List of field names to convert to predictions
        project: Optional project instance to determine correct to_name and type from label config
    """
    new_tasks = []
    validation_errors = []

    # If project is provided, create LabelInterface to determine correct mappings
    li = None
    if project:
        try:
            li = LabelInterface(project.label_config)
        except Exception as e:
            logger.warning(f'Could not create LabelInterface for project {project.id}: {e}')

    for task_index, task in enumerate(tasks):
        if 'data' in task:
            task_data = task['data']
        else:
            task_data = task

        predictions = []
        for field in preannotated_from_fields:
            if field not in task_data:
                validation_errors.append(f"Task {task_index}: Preannotated field '{field}' not found in task data")
                continue

            value = task_data[field]
            if value is not None:
                # Try to determine correct to_name and type from project configuration
                to_name = 'text'  # Default fallback
                prediction_type = 'choices'  # Default fallback

                if li:
                    # Find a control tag that matches the field name
                    try:
                        control_tag = li.get_control(field)
                        # Use the control's to_name and determine type
                        if hasattr(control_tag, 'to_name') and control_tag.to_name:
                            to_name = (
                                control_tag.to_name[0]
                                if isinstance(control_tag.to_name, list)
                                else control_tag.to_name
                            )
                            prediction_type = control_tag.tag.lower()
                    except Exception:
                        # Control not found, use defaults
                        pass

                # Create prediction from preannotated field
                # Handle different types of values
                if isinstance(value, dict):
                    # For complex structures like bounding boxes, use the value directly
                    prediction_value = value
                else:
                    # For simple values, use the prediction_type as the key
                    # Handle cases where the type doesn't match the expected key
                    value_key = prediction_type
                    if prediction_type == 'textarea':
                        value_key = 'text'

                    # Most types expect lists, but some expect single values
                    if prediction_type in ['rating', 'number', 'datetime']:
                        prediction_value = {value_key: value}
                    else:
                        # Wrap in list for most types
                        prediction_value = {value_key: [value] if not isinstance(value, list) else value}

                prediction = {
                    'result': [
                        {
                            'from_name': field,
                            'to_name': to_name,
                            'type': prediction_type,
                            'value': prediction_value,
                        }
                    ],
                    'score': 1.0,
                    'model_version': 'preannotated',
                }

                predictions.append(prediction)

        # Create new task structure
        new_task = {'data': task_data, 'predictions': predictions}
        new_tasks.append(new_task)

    # If there are validation errors, raise them
    if validation_errors and raise_errors:
        raise ValidationError({'preannotated_fields': validation_errors})

    return new_tasks


post_process_reimport = load_func(settings.POST_PROCESS_REIMPORT)


def _require_no_external_export_links(project, file_upload_ids):
    """Don't let task deletion synchronously remove external annotation exports.

    Check even disabled deletion settings: those settings can change while a
    reimport is staging. Built-in export writers also lock the project row.
    """
    from io_storages.base_models import ExportStorageLink

    for model in apps.get_models():
        if issubclass(model, ExportStorageLink) and not model._meta.abstract:
            if model.objects.filter(
                annotation__task__project=project,
                annotation__task__file_upload_id__in=file_upload_ids,
            ).exists():
                raise ValidationError('Reimport cannot remove tasks with external export links.')


def _async_reimport_background_streaming(reimport, project, organization_id, user, *, streaming=True):
    """Stage input before the project lock, then commit the entire replacement."""
    try:
        with transaction.atomic():
            principal = local_principal(user)
            project, user = lock_project_operation(project.pk, principal, MANAGE_TASKS)
            authorization.require(
                authorization.can_manage_project(principal, project),
                'Project manager role is required to reimport tasks.',
            )
        if not reimport.file_upload_ids:
            # Legacy queued jobs may contain an empty list. Treat it as a no-op;
            # the loader interprets an empty list as every file in the project.
            with transaction.atomic():
                principal = local_principal(user)
                project, _ = lock_project_operation(project.pk, principal, MANAGE_TASKS)
                authorization.require(
                    authorization.can_manage_project(principal, project),
                    'Project manager role is required to reimport tasks.',
                )
                reimport.status = ProjectReimport.Status.COMPLETED
                reimport.save(update_fields=['status'])
            return
        requested_ids = set(reimport.file_upload_ids)
        source_files = dict(
            FileUpload.objects.filter(project=project, pk__in=requested_ids).values_list('pk', 'file')
        )
        source_config = project.label_config
        if set(source_files) != requested_ids:
            raise ValidationError('Reimport files are missing or outside the project.')
        with tempfile.TemporaryFile(mode='w+t', encoding='utf-8') as staged:
            if streaming:
                source_batches = FileUpload.load_tasks_from_uploaded_files_streaming(
                    project, reimport.file_upload_ids,
                    files_as_tasks_list=reimport.files_as_tasks_list, batch_size=settings.REIMPORT_BATCH_SIZE,
                )
            else:
                source_batches = [FileUpload.load_tasks_from_uploaded_files(
                    project, reimport.file_upload_ids, files_as_tasks_list=reimport.files_as_tasks_list,
                )]
            for tasks, formats, columns in source_batches:
                staged.write(json.dumps([tasks, formats, list(columns)], allow_nan=False) + '\n')
            staged.seek(0)

            def batches():
                for line in staged:
                    tasks, formats, columns = json.loads(line)
                    yield tasks, formats, set(columns)

            with transaction.atomic():
                principal = local_principal(user)
                project, user = lock_project_operation(project.pk, principal, MANAGE_TASKS)
                authorization.require(
                    authorization.can_manage_project(principal, project),
                    'Project manager role is required to reimport tasks.',
                )
                current_files = dict(
                    FileUpload.objects.select_for_update()
                    .filter(project=project, pk__in=requested_ids)
                    .values_list('pk', 'file')
                )
                if current_files != source_files or project.label_config != source_config:
                    raise ValidationError('Reimport files or project configuration changed during staging.')
                _require_no_external_export_links(project, requested_ids)
                _commit_streaming_reimport(reimport, project, organization_id, user, batches())
    except Exception as exc:
        # Any source changes and the completed receipt have rolled back here.
        ProjectReimport.objects.filter(pk=reimport.pk).update(
            status=ProjectReimport.Status.FAILED, traceback=traceback.format_exc(), error=str(exc)
        )
        raise


def _commit_streaming_reimport(reimport, project, organization_id, user, staged_batches):
    """Streaming version of reimport that processes tasks in batches to reduce memory usage"""
    try:
        # Initialize counters
        total_task_count = 0
        total_annotation_count = 0
        total_prediction_count = 0
        all_found_formats = {}
        all_data_columns = set()
        all_created_task_ids = []

        # Remove old tasks once before starting
        with transaction.atomic():
            project.remove_tasks_by_file_uploads(reimport.file_upload_ids)

        # Process tasks in batches
        batch_number = 0
        for batch_tasks, batch_formats, batch_columns in staged_batches:
            if not batch_tasks:
                logger.info(f'Empty batch received for reimport {reimport.id}')
                continue

            batch_number += 1
            logger.info(f'Processing batch {batch_number} with {len(batch_tasks)} tasks for reimport {reimport.id}')

            # Process batch in transaction
            with transaction.atomic():
                # Lock summary for update to avoid race conditions
                summary = ProjectSummary.objects.select_for_update().get(project=project)

                # Serialize and save batch
                serializer = ImportApiSerializer(
                    data=batch_tasks, many=True, context={'project': project, 'user': user}
                )
                serializer.is_valid(raise_exception=True)
                batch_db_tasks = serializer.save(project_id=project.id)

                # Collect task IDs for later use
                all_created_task_ids.extend([t.id for t in batch_db_tasks])

                # Update batch counters
                batch_task_count = len(batch_db_tasks)
                batch_annotation_count = len(serializer.db_annotations)
                batch_prediction_count = len(serializer.db_predictions)

                total_task_count += batch_task_count
                total_annotation_count += batch_annotation_count
                total_prediction_count += batch_prediction_count

                # Update formats and columns
                all_found_formats.update(batch_formats)
                if batch_columns:
                    if not all_data_columns:
                        all_data_columns = batch_columns
                    else:
                        all_data_columns &= batch_columns

                # Update data columns in summary
                summary.update_data_columns(batch_db_tasks)

            logger.info(
                f'Batch {batch_number} processed successfully: {batch_task_count} tasks, '
                f'{batch_annotation_count} annotations, {batch_prediction_count} predictions'
            )

        # After all batches are processed, emit webhooks and update task states once
        if all_created_task_ids:
            logger.info(
                f'Finalizing reimport: emitting webhooks and updating task states for {len(all_created_task_ids)} tasks'
            )

            # Emit webhooks for all tasks at once (passing list of IDs)
            if getattr(settings, 'ANNOTATION_STORAGE_ADMISSION_ENABLED', False):
                from webhooks.durable import queue_task_created_webhooks

                queue_task_created_webhooks(project, Task.objects.filter(id__in=all_created_task_ids).order_by('pk'))
            else:
                transaction.on_commit(
                    lambda: emit_webhooks_for_instance(
                        organization_id, project, WebhookAction.TASKS_CREATED, all_created_task_ids
                    ),
                    robust=True,
                )

            # Update task states for all tasks at once
            all_tasks_queryset = Task.objects.filter(id__in=all_created_task_ids)
            recalculate_stats_counts = {
                'task_count': total_task_count,
                'annotation_count': total_annotation_count,
                'prediction_count': total_prediction_count,
            }

            project.update_tasks_counters_and_task_states(
                tasks_queryset=all_tasks_queryset,
                maximum_annotations_changed=False,
                overlap_cohort_percentage_changed=False,
                tasks_number_changed=True,
                recalculate_stats_counts=recalculate_stats_counts,
            )
            logger.info('Tasks bulk_update finished (async streaming reimport)')

        # Update reimport with final statistics
        reimport.task_count = total_task_count
        reimport.annotation_count = total_annotation_count
        reimport.prediction_count = total_prediction_count
        reimport.found_formats = all_found_formats
        reimport.data_columns = list(all_data_columns)
        reimport.status = ProjectReimport.Status.COMPLETED
        reimport.save()

        logger.info(f'Streaming reimport {reimport.id} completed: {total_task_count} tasks imported')

        # Run post-processing
        transaction.on_commit(lambda: post_process_reimport(reimport), robust=True)

    except Exception as e:
        logger.error(f'Error in streaming reimport {reimport.id}: {str(e)}', exc_info=True)
        raise


def _async_import_background_streaming(project_import, user, actor=None):
    actor = actor or user
    try:
        if project_import.commit_to_project:
            with transaction.atomic():
                principal = local_principal(actor)
                project, actor = lock_project_operation(project_import.project_id, principal, MANAGE_TASKS)
                authorization.require(
                    authorization.can_manage_project(principal, project),
                    'Project manager role is required to import tasks.',
                )
                project_import.project = project
                source_config = project.label_config
        # Input parsing may access remote storage; finish it before taking the
        # project row lock. TemporaryFile is private and removed on close.
        with tempfile.TemporaryFile(mode='w+t', encoding='utf-8') as staged:
            for tasks, ids, formats, columns in load_tasks_for_async_import_streaming(
                project_import, user, settings.IMPORT_BATCH_SIZE
            ):
                staged.write(json.dumps([tasks, ids, formats, list(columns)], allow_nan=False) + '\n')
            staged.seek(0)

            def batches():
                for line in staged:
                    yield json.loads(line)

            with transaction.atomic():
                if project_import.commit_to_project:
                    principal = local_principal(actor)
                    project, actor = lock_project_operation(project_import.project_id, principal, MANAGE_TASKS)
                    authorization.require(
                        authorization.can_manage_project(principal, project),
                        'Project manager role is required to import tasks.',
                    )
                    if project.label_config != source_config:
                        raise ValidationError('Project label configuration changed during import; retry.')
                    project_import.project = project
                _commit_streaming_import(project_import, user, actor, batches())
    except Exception as exc:
        ProjectImport.objects.filter(pk=project_import.pk).update(
            status=ProjectImport.Status.FAILED, traceback=traceback.format_exc(), error=str(exc)
        )
        raise


def _commit_streaming_import(project_import, user, actor, staged_batches):
    try:
        total_task_count = 0
        total_annotation_count = 0
        total_prediction_count = 0
        all_created_task_ids = []

        project = project_import.project
        start = time.time()

        batch_number = 0
        final_file_upload_ids = []
        final_found_formats = {}
        final_data_columns = set()

        for batch_tasks, file_upload_ids, found_formats, data_columns in staged_batches:
            if not batch_tasks:
                logger.info(f'Empty batch received for import {project_import.id}')
                continue

            batch_number += 1
            logger.info(
                f'Processing batch {batch_number} with {len(batch_tasks)} tasks for import {project_import.id}'
            )

            if file_upload_ids and file_upload_ids not in final_file_upload_ids:
                final_file_upload_ids = file_upload_ids
            final_found_formats.update(found_formats)
            final_data_columns.update(data_columns)

            if project_import.preannotated_from_fields:
                raise_errors = flag_set(
                    'fflag_feat_utc_210_prediction_validation_15082025', user=project.organization.created_by
                )
                logger.info(f'Reformatting predictions with raise_errors: {raise_errors}')
                batch_tasks = reformat_predictions(
                    batch_tasks, project_import.preannotated_from_fields, project, raise_errors
                )

            if project.label_config_is_not_default and flag_set(
                'fflag_feat_utc_210_prediction_validation_15082025', user=project.organization.created_by
            ):
                validation_errors = []
                li = LabelInterface(project.label_config)

                for i, task in enumerate(batch_tasks):
                    if 'predictions' in task:
                        for j, prediction in enumerate(task['predictions']):
                            try:
                                prediction = sanitize_prediction_import_payload(prediction)
                                validation_errors_list = li.validate_prediction(prediction, return_errors=True)
                                if validation_errors_list:
                                    for error in validation_errors_list:
                                        validation_errors.append(
                                            f'Task {total_task_count + i}, prediction {j}: {error}'
                                        )
                            except Exception as e:
                                error_msg = f'Task {total_task_count + i}, prediction {j}: Error validating prediction - {str(e)}'
                                validation_errors.append(error_msg)
                                logger.error(f'Exception during validation: {error_msg}')

                if validation_errors:
                    error_message = f'Prediction validation failed ({len(validation_errors)} errors):\n'
                    for error in validation_errors:
                        error_message += f'- {error}\n'

                    if flag_set(
                        'fflag_feat_utc_210_prediction_validation_15082025', user=project.organization.created_by
                    ):
                        raise ValidationError(error_message)
                    else:
                        logger.error(
                            f'Prediction validation failed, not raising error - ({len(validation_errors)} errors):\n{error_message}'
                        )

            if project_import.commit_to_project:
                with transaction.atomic():
                    summary = ProjectSummary.objects.select_for_update().get(project=project)

                    serializer = ImportApiSerializer(data=batch_tasks, many=True, context={'project': project, 'user': actor})
                    serializer.is_valid(raise_exception=True)
                    batch_db_tasks = serializer.save(project_id=project.id)

                    all_created_task_ids.extend([t.id for t in batch_db_tasks])

                    batch_task_count = len(batch_db_tasks)
                    batch_annotation_count = len(serializer.db_annotations)
                    batch_prediction_count = len(serializer.db_predictions)

                    total_task_count += batch_task_count
                    total_annotation_count += batch_annotation_count
                    total_prediction_count += batch_prediction_count

                    summary.update_data_columns(batch_db_tasks)

            else:
                total_task_count += len(batch_tasks)

            logger.info(f'Batch {batch_number} processed successfully: {len(batch_tasks)} tasks')

        final_data_columns = list(final_data_columns)

        if project_import.commit_to_project and all_created_task_ids:
            logger.info(
                f'Finalizing import: emitting webhooks and updating task states for {len(all_created_task_ids)} tasks'
            )

            if getattr(settings, 'ANNOTATION_STORAGE_ADMISSION_ENABLED', False):
                from webhooks.durable import queue_task_created_webhooks

                queue_task_created_webhooks(project, Task.objects.filter(id__in=all_created_task_ids).order_by('pk'))
            else:
                transaction.on_commit(
                    lambda: emit_webhooks_for_instance(
                        project.organization, project, WebhookAction.TASKS_CREATED, all_created_task_ids
                    ),
                    robust=True,
                )

            recalculate_stats_counts = {
                'task_count': total_task_count,
                'annotation_count': total_annotation_count,
                'prediction_count': total_prediction_count,
            }

            all_tasks_queryset = Task.objects.filter(id__in=all_created_task_ids)
            project.update_tasks_counters_and_task_states(
                tasks_queryset=all_tasks_queryset,
                maximum_annotations_changed=False,
                overlap_cohort_percentage_changed=False,
                tasks_number_changed=True,
                recalculate_stats_counts=recalculate_stats_counts,
            )
            logger.info('Tasks bulk_update finished (async streaming import)')

        duration = time.time() - start

        project_import.task_count = total_task_count or 0
        project_import.annotation_count = total_annotation_count or 0
        project_import.prediction_count = total_prediction_count or 0
        project_import.duration = duration
        project_import.file_upload_ids = final_file_upload_ids
        project_import.found_formats = final_found_formats
        project_import.data_columns = final_data_columns
        if project_import.return_task_ids:
            project_import.task_ids = all_created_task_ids

        project_import.status = ProjectImport.Status.COMPLETED
        project_import.save()

        logger.info(f'Streaming import {project_import.id} completed: {total_task_count} tasks imported')

    except Exception as e:
        logger.error(f'Error in streaming import {project_import.id}: {str(e)}', exc_info=True)
        raise


def async_reimport_background(reimport_id, organization_id, user_id, actor_id=None, **kwargs):

    with transaction.atomic():
        try:
            reimport = ProjectReimport.objects.select_for_update().get(id=reimport_id)
        except ProjectReimport.DoesNotExist:
            logger.error(f'ProjectReimport with id {reimport_id} not found, import processing failed')
            return
        if reimport.status != ProjectReimport.Status.CREATED:
            logger.error(f'Processing reimport with id {reimport_id} already started')
            return
        reimport.status = ProjectReimport.Status.IN_PROGRESS
        reimport.save(update_fields=['status'])

    project = reimport.project
    # Backward compatibility for jobs queued by older code that serialized
    # the User object directly instead of its primary key.
    user = user_id if isinstance(user_id, User) else User.objects.get(id=user_id)
    actor = User.objects.get(id=actor_id) if actor_id is not None else user

    # Both parsing modes share the project-first atomic replacement and receipt.
    streaming = flag_set('fflag_fix_back_plt_838_reimport_memory_improvement_05082025_short', user='auto')
    _async_reimport_background_streaming(reimport, project, organization_id, actor, streaming=streaming)
