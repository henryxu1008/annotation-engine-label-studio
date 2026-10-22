"""This file and its contents are licensed under the Apache License 2.0. Please see the included NOTICE for copyright information and LICENSE for a copy of the license.
"""
import logging

from access_control.authorization import authorization
from access_control.identity import resolve_principal
from access_control.project_operations import EDIT, require_project_operation
from core.permissions import all_permissions
from data_manager.actions import DataManagerAction
from data_manager.functions import filters_ordering_selected_items_exist
from projects.functions.next_task import get_next_task
from rest_framework.exceptions import NotFound
from task_packages.scope import PackageScope
from tasks.serializers import NextTaskSerializer

logger = logging.getLogger(__name__)


def next_task(project, queryset, **kwargs):
    """Generate next task for labeling stream

    :param project: project
    :param queryset: task ids to sample from
    :param kwargs: arguments from api request
    """

    request = kwargs['request']
    scope = PackageScope.from_request(request, project)
    if scope:
        require_project_operation(scope.project, EDIT)
        queryset = scope.tasks(queryset)
    dm_queue = filters_ordering_selected_items_exist(request.data)
    next_task, queue_info = get_next_task(scope.actor if scope else request.user, queryset, project, dm_queue)

    if scope:
        scope = PackageScope.from_request(request, project)
        require_project_operation(scope.project, EDIT)

    if next_task is None:
        error = NotFound('No tasks are currently available in this queue.')
        error.display_context = {'reason': 'QUEUE_EXHAUSTED'}
        raise error

    authorization.require(
        next_task.project_id == project.id and authorization.can_view_task(resolve_principal(request), next_task),
        'The assigned task is no longer available.',
    )

    # serialize task
    context = {'request': request, 'project': project, 'resolve_uri': True, 'annotations': False}
    serializer = NextTaskSerializer(next_task, context=context)
    response = serializer.data
    if scope:
        scope = PackageScope.from_request(request, project)
        require_project_operation(scope.project, EDIT)
        scope.require_task(next_task)
    response['queue'] = queue_info
    return response


actions: list[DataManagerAction] = [
    {
        'entry_point': next_task,
        'permission': all_permissions.projects_view,
        'title': 'Generate Next Task',
        'order': 0,
        'hidden': True,
    }
]
