from core.utils.exceptions import LabelStudioAPIException
from rest_framework import status


class AnnotationDuplicateError(LabelStudioAPIException):
    status_code = status.HTTP_409_CONFLICT
    default_detail = 'Annotation with this unique id already exists'


class AssignmentConflictError(LabelStudioAPIException):
    status_code = status.HTTP_409_CONFLICT
    default_detail = 'Task assignment changed. Reload the task before saving.'


class DraftConflictError(LabelStudioAPIException):
    status_code = status.HTTP_409_CONFLICT
    default_detail = 'This draft changed in another page. Keep a copy of your edits and reload the latest draft.'
    display_context = {'reason': 'DRAFT_CONFLICT'}
