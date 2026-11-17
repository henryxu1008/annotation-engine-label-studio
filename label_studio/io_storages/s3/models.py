"""This file and its contents are licensed under the Apache License 2.0. Please see the included NOTICE for copyright information and LICENSE for a copy of the license.
"""

import json
import logging
import re
from typing import Union
from urllib.parse import urlparse

import boto3
from core.feature_flags import flag_set
from core.redis import start_job_async_or_sync
from django.conf import settings
from django.core.validators import RegexValidator
from django.db import models, transaction
from django.db.models.signals import post_save, pre_delete
from django.dispatch import receiver
from django.utils.translation import gettext_lazy as _
from io_storages.admission import admitted_annotation_effect
from io_storages.base_models import (
    ExportStorage,
    ExportStorageLink,
    ImportStorage,
    ImportStorageLink,
    ProjectStorageMixin,
)
from io_storages.s3.utils import (
    catch_and_reraise_from_none,
    get_client_and_resource,
    resolve_s3_url,
)
from io_storages.utils import StorageObject, load_tasks_json, storage_can_resolve_bucket_url
from rest_framework.exceptions import PermissionDenied
from tasks.models import Annotation

from label_studio.io_storages.s3.utils import AWS

logger = logging.getLogger(__name__)
logging.getLogger('botocore').setLevel(logging.CRITICAL)
boto3.set_stream_logger(level=logging.INFO)

clients_cache = {}


class S3StorageMixin(models.Model):
    bucket = models.TextField(_('bucket'), null=True, blank=True, help_text='S3 bucket name')
    prefix = models.TextField(_('prefix'), null=True, blank=True, help_text='S3 bucket prefix')
    regex_filter = models.TextField(
        _('regex_filter'),
        null=True,
        blank=True,
        help_text='Cloud storage regex for filtering objects',
    )
    use_blob_urls = models.BooleanField(
        _('use_blob_urls'),
        default=False,
        help_text='Interpret objects as BLOBs and generate URLs',
    )
    aws_access_key_id = models.TextField(_('aws_access_key_id'), null=True, blank=True, help_text='AWS_ACCESS_KEY_ID')
    aws_secret_access_key = models.TextField(
        _('aws_secret_access_key'),
        null=True,
        blank=True,
        help_text='AWS_SECRET_ACCESS_KEY',
    )
    aws_session_token = models.TextField(_('aws_session_token'), null=True, blank=True, help_text='AWS_SESSION_TOKEN')
    aws_sse_kms_key_id = models.TextField(
        _('aws_sse_kms_key_id'), null=True, blank=True, help_text='AWS SSE KMS Key ID'
    )
    region_name = models.TextField(_('region_name'), null=True, blank=True, help_text='AWS Region')
    s3_endpoint = models.TextField(_('s3_endpoint'), null=True, blank=True, help_text='S3 Endpoint')

    @catch_and_reraise_from_none
    def get_client_and_resource(self, *, single_attempt=False):
        # s3 client initialization ~ 100 ms, for 30 tasks it's a 3 seconds, so we need to cache it
        cache_key = f'{self.aws_access_key_id}:{self.aws_secret_access_key}:{self.aws_session_token}:{self.region_name}:{self.s3_endpoint}:{single_attempt}'
        if cache_key in clients_cache:
            return clients_cache[cache_key]

        result = get_client_and_resource(
            self.aws_access_key_id,
            self.aws_secret_access_key,
            self.aws_session_token,
            self.region_name,
            self.s3_endpoint,
            single_attempt=single_attempt,
        )
        clients_cache[cache_key] = result
        return result

    def get_client(self):
        client, _ = self.get_client_and_resource()
        return client

    def get_client_and_bucket(self, validate_connection=True):
        client, s3 = self.get_client_and_resource()
        if validate_connection:
            self.validate_connection(client)
        return client, s3.Bucket(self.bucket)

    @catch_and_reraise_from_none
    def validate_connection(self, client=None):
        logger.debug('validate_connection')
        if client is None:
            client = self.get_client()
        # TODO(jo): add check for write access for .*Export.* classes
        is_export = 'Export' in self.__class__.__name__
        if self.prefix:
            logger.debug(
                f'[Class {self.__class__.__name__}]: Test connection to bucket {self.bucket} with prefix {self.prefix} using ListObjectsV2 operation'
            )
            result = client.list_objects_v2(Bucket=self.bucket, Prefix=self.prefix, MaxKeys=1)
            # We expect 1 key with the prefix for imports. For exports it's okay if there are 0 with the prefix.
            expected_keycount = 0 if is_export else 1
            if (keycount := result.get('KeyCount')) is None or keycount < expected_keycount:
                raise KeyError(f'{self.url_scheme}://{self.bucket}/{self.prefix} not found.')
        else:
            logger.debug(
                f'[Class {self.__class__.__name__}]: Test connection to bucket {self.bucket} using HeadBucket operation'
            )
            client.head_bucket(Bucket=self.bucket)

    @property
    def path_full(self):
        prefix = self.prefix or ''
        return f'{self.url_scheme}://{self.bucket}/{prefix}'

    @property
    def type_full(self):
        return 'Amazon AWS S3'

    @catch_and_reraise_from_none
    def get_bytes_stream(self, uri, range_header=None):
        """Get file directly from S3 using iter_chunks without wrapper.

        This method forwards Range headers directly to S3 and returns the raw stream.
        Note: The returned stream is NOT seekable and will break if seeking backwards.

        Args:
            uri: The S3 URI of the file to retrieve
            range_header: Optional HTTP Range header to forward to S3

        Returns:
            Tuple of (stream, content_type, metadata) where metadata contains
            important S3 headers like ETag, ContentLength, etc.
        """
        # Parse URI to get bucket and key
        parsed_uri = urlparse(uri, allow_fragments=False)
        bucket_name = parsed_uri.netloc
        key = parsed_uri.path.lstrip('/')

        # Get S3 client
        client = self.get_client()

        try:
            # Forward Range header to S3 if provided
            request_params = {'Bucket': bucket_name, 'Key': key}
            if range_header:
                request_params['Range'] = range_header

            # Get the object from S3
            response = client.get_object(**request_params)

            # Extract metadata to return
            metadata = {
                'ETag': response.get('ETag'),
                'ContentLength': response.get('ContentLength'),
                'ContentRange': response.get('ContentRange'),
                'LastModified': response.get('LastModified'),
                'StatusCode': response['ResponseMetadata']['HTTPStatusCode'],
            }

            # Return the streaming body directly
            return response['Body'], response.get('ContentType'), metadata

        except Exception as e:
            logger.error(f'Error getting direct stream from S3 for uri {uri}: {e}', exc_info=True)
            return None, None, {}

    class Meta:
        abstract = True


class S3ImportStorageBase(S3StorageMixin, ImportStorage):

    url_scheme = 's3'

    presign = models.BooleanField(_('presign'), default=True, help_text='Generate presigned URLs')
    presign_ttl = models.PositiveSmallIntegerField(
        _('presign_ttl'), default=1, help_text='Presigned URLs TTL (in minutes)'
    )
    recursive_scan = models.BooleanField(
        _('recursive scan'),
        default=False,
        help_text=_('Perform recursive scan over the bucket content'),
    )

    @catch_and_reraise_from_none
    def iter_objects(self):
        _, bucket = self.get_client_and_bucket()
        list_kwargs = {}
        if self.prefix:
            list_kwargs['Prefix'] = self.prefix.rstrip('/') + '/'
        if not self.recursive_scan:
            list_kwargs['Delimiter'] = '/'
        bucket_iter = bucket.objects.filter(**list_kwargs).all()
        regex = re.compile(str(self.regex_filter)) if self.regex_filter else None
        for obj in bucket_iter:
            key = obj.key
            if key.endswith('/'):
                logger.debug(key + ' is skipped because it is a folder')
                continue
            if regex and not regex.match(key):
                logger.debug(key + ' is skipped by regex filter')
                continue
            logger.debug(f's3 {key} has passed the regex filter')
            yield obj

    @catch_and_reraise_from_none
    def iter_keys(self):
        for obj in self.iter_objects():
            yield obj.key

    def get_unified_metadata(self, obj):
        return {
            'key': obj.key,
            'last_modified': obj.last_modified,
            'size': obj.size,
        }

    @catch_and_reraise_from_none
    def scan_and_create_links(self):
        return self._scan_and_create_links(S3ImportStorageLink)

    @catch_and_reraise_from_none
    def get_data(self, key) -> list[StorageObject]:
        uri = f'{self.url_scheme}://{self.bucket}/{key}'
        if self.use_blob_urls:
            data_key = settings.DATA_UNDEFINED_NAME
            task = {data_key: uri}
            return [StorageObject(key=key, task_data=task)]

        # read task json from bucket and validate it
        _, s3 = self.get_client_and_resource()
        bucket = s3.Bucket(self.bucket)
        obj = s3.Object(bucket.name, key).get()['Body'].read()
        return load_tasks_json(obj, key)

    @catch_and_reraise_from_none
    def generate_http_url(self, url):
        return resolve_s3_url(url, self.get_client(), self.presign, expires_in=self.presign_ttl * 60)

    @catch_and_reraise_from_none
    def can_resolve_url(self, url: Union[str, None]) -> bool:
        return storage_can_resolve_bucket_url(self, url)

    @catch_and_reraise_from_none
    def get_blob_metadata(self, key):
        return AWS.get_blob_metadata(
            key,
            self.bucket,
            aws_access_key_id=self.aws_access_key_id,
            aws_secret_access_key=self.aws_secret_access_key,
            aws_session_token=self.aws_session_token,
            region_name=self.region_name,
            s3_endpoint=self.s3_endpoint,
        )

    class Meta:
        abstract = True


class S3ImportStorage(ProjectStorageMixin, S3ImportStorageBase):
    class Meta:
        abstract = False


class S3ExportStorage(S3StorageMixin, ExportStorage):
    expected_bucket_owner = models.CharField(
        max_length=12, null=True, blank=True,
        validators=[RegexValidator(r'^[0-9]{12}$', 'Enter a 12-digit AWS account ID.')],
        help_text='Expected AWS bucket owner account ID for owner-bound exports.',
    )

    @admitted_annotation_effect('annotation_export')
    @catch_and_reraise_from_none
    @transaction.atomic
    def save_annotation(self, annotation, *, _issue86_export_identity=None):
        self.require_mutable_delivery_allowed(lock_project=True)
        durable_export = _issue86_export_identity is not None or getattr(settings, 'ANNOTATION_STORAGE_ADMISSION_ENABLED', False)
        if durable_export:
            # The outbox worker can load this object before another committed
            # edit. Refresh under the project lock before deriving the key or
            # serializing, so an older in-memory instance cannot overwrite it.
            annotation.refresh_from_db()
            if annotation.project_id != self.project_id:
                raise ValueError('The annotation project changed before the S3 request.')
        # get key that identifies this object in storage
        key = S3ExportStorageLink.get_key(annotation)
        key = str(self.prefix) + '/' + key if self.prefix else key
        if durable_export:
            identity = _issue86_export_identity
            if (not isinstance(identity, dict) or identity.get('key') != key
                    or identity.get('annotation_id') != annotation.pk or identity.get('storage_id') != self.pk
                    or identity.get('project_id') != self.project_id):
                raise ValueError('A durable S3 export request identity is required.')
            from django.utils.crypto import constant_time_compare
            from io_storages.s3.version_provenance import storage_binding

            if identity['binding'] and not constant_time_compare(storage_binding(self) or '', identity['binding']):
                raise ValueError('The S3 storage binding changed before the request.')
        client, s3 = (self.get_client_and_resource(single_attempt=True) if durable_export
                      else self.get_client_and_resource())
        logger.debug(f'Creating new object on {self.__class__.__name__} Storage {self} for annotation {annotation}')
        ser_annotation = self._get_serialized_data(annotation)

        # put object into storage
        additional_params = {}
        object_token = None
        if durable_export:
            object_token = identity['token']
            additional_params['Metadata'] = {'ls-export-token': object_token}
            if self.expected_bucket_owner:
                if not re.fullmatch(r'[0-9]{12}', self.expected_bucket_owner):
                    raise ValueError('Invalid expected AWS bucket owner.')
                additional_params['ExpectedBucketOwner'] = self.expected_bucket_owner

        self.cached_user = getattr(self, 'cached_user', self.project.organization.created_by)
        if flag_set(
            'fflag_feat_back_lsdv_3958_server_side_encryption_for_target_storage_short',
            user=self.cached_user,
        ):
            if self.aws_sse_kms_key_id:
                additional_params['SSEKMSKeyId'] = self.aws_sse_kms_key_id
                additional_params['ServerSideEncryption'] = 'aws:kms'
            else:
                additional_params['ServerSideEncryption'] = 'AES256'

        response = s3.Object(self.bucket, key).put(Body=json.dumps(ser_annotation), **additional_params)

        # create link if everything ok
        link = S3ExportStorageLink.create(annotation, self)
        if durable_export:
            from io_storages.s3.version_provenance import put_version

            version = put_version(response)
            identity['observed_version'] = version
            binding = identity['binding'] if version else None
            # A missing/unsafe response must invalidate an older pin.
            link.pinned_version_id = version if binding else None
            link.pinned_key = key if binding else None
            link.pinned_storage_binding = binding
            link.pinned_object_token = object_token if binding else None
            link.save(update_fields=['pinned_version_id', 'pinned_key', 'pinned_storage_binding', 'pinned_object_token'])
        elif link.pinned_version_id or link.pinned_key or link.pinned_storage_binding or link.pinned_object_token:
            # A legacy-mode overwrite must not leave an earlier version pin live.
            link.pinned_version_id = link.pinned_key = link.pinned_storage_binding = link.pinned_object_token = None
            link.save(update_fields=['pinned_version_id', 'pinned_key', 'pinned_storage_binding', 'pinned_object_token'])

    @admitted_annotation_effect('annotation_delete')
    def delete_annotation(self, annotation):
        # A key-only DELETE can hide a replacement. The old path has no durable
        # version or outcome, even when lifecycle admission is disabled.
        raise PermissionDenied('Unversioned S3 annotation deletion is unavailable.')


def async_export_annotation_to_s3_storages(annotation: 'Annotation | int'):
    if isinstance(annotation, int):
        try:
            annotation = Annotation.objects.get(pk=annotation)
        except Annotation.DoesNotExist:
            logger.info(f'Annotation {annotation} no longer exists, skipping S3 export')
            return

    project = annotation.project
    if hasattr(project, 'io_storages_s3exportstorages'):
        for storage in project.io_storages_s3exportstorages.all():
            if storage.has_formal_submissions():
                logger.info(
                    f'Skip mutable S3 export for annotation {annotation.id}: formal submissions exist'
                )
                continue
            logger.debug(f'Export {annotation} to S3 storage {storage}')
            storage.save_annotation(annotation)


def _dispatch_s3_annotation_export(annotation_id):
    try:
        start_job_async_or_sync(async_export_annotation_to_s3_storages, annotation_id)
    except Exception:
        logger.exception(f'Post-commit export_annotation_to_s3_storages dispatch failed for annotation {annotation_id}')


@receiver(post_save, sender=Annotation)
def export_annotation_to_s3_storages(sender, instance, **kwargs):
    if getattr(settings, 'ANNOTATION_STORAGE_ADMISSION_ENABLED', False):
        from io_storages.annotation_outbox import queue_annotation_exports

        queue_annotation_exports(instance, S3ExportStorage)
        return
    storages = getattr(instance.project, 'io_storages_s3exportstorages', None)
    if storages and storages.exists():  # avoid excess jobs in rq
        annotation_id = instance.pk
        transaction.on_commit(lambda annotation_id=annotation_id: _dispatch_s3_annotation_export(annotation_id))


@receiver(pre_delete, sender=Annotation)
def delete_annotation_from_s3_storages(sender, instance, **kwargs):
    if getattr(settings, 'ANNOTATION_STORAGE_ADMISSION_ENABLED', False):
        # The version pin, storage configuration and derived key must be read
        # after acquiring the same project lock held by admitted exports. A
        # queryset evaluated before the lock can retain an obsolete pin even
        # when the concurrent PUT has completed by the time admission runs.
        if not transaction.get_connection().in_atomic_block:
            raise RuntimeError('Annotation deletion requires a database transaction')
        from projects.models import Project

        Project.objects.select_for_update(nowait=True).get(pk=instance.project_id)
        current = Annotation.objects.filter(pk=instance.pk, project_id=instance.project_id).values('task_id').first()
        if current is None or current['task_id'] != instance.task_id:
            raise ValueError('Annotation identity changed before S3 deletion admission.')
    links = S3ExportStorageLink.objects.filter(annotation=instance)
    for link in links:
        storage = link.storage
        if storage.can_delete_objects:
            if getattr(settings, 'ANNOTATION_STORAGE_ADMISSION_ENABLED', False):
                from io_storages.annotation_outbox import queue_annotation_deletion

                key = S3ExportStorageLink.get_key(instance)
                key = str(storage.prefix) + '/' + key if storage.prefix else key
                from io_storages.s3.version_provenance import put_version, storage_binding

                binding = storage_binding(storage)
                token = link.pinned_object_token
                if not binding or binding != link.pinned_storage_binding or key != link.pinned_key or not token:
                    version = binding = token = None
                else:
                    version = put_version({'VersionId': link.pinned_version_id})
                    if version is None:
                        binding = token = None
                queue_annotation_deletion(
                    instance, storage, key, target_version=version, target_binding=binding, target_token=token,
                )
                continue
            logger.debug(f'Delete {instance} from S3 storage {storage}')  # nosec
            storage.delete_annotation(instance)


class S3ImportStorageLink(ImportStorageLink):
    storage = models.ForeignKey(S3ImportStorage, on_delete=models.CASCADE, related_name='links')

    @classmethod
    def exists(cls, keys, storage) -> set[str]:
        super_exists = super(S3ImportStorageLink, cls).exists
        # TODO: this is a workaround to be compatible with old keys version - remove it later
        prefix = str(storage.prefix) or ''
        if prefix:
            return (
                super_exists(keys, storage)
                | super_exists([prefix + key for key in keys], storage)
                | super_exists([prefix + '/' + key for key in keys], storage)
            )
        else:
            return super_exists(keys, storage)


class S3ExportStorageLink(ExportStorageLink):
    storage = models.ForeignKey(S3ExportStorage, on_delete=models.CASCADE, related_name='links')
    pinned_version_id = models.TextField(null=True, blank=True)
    pinned_key = models.TextField(null=True, blank=True)
    pinned_storage_binding = models.CharField(max_length=64, null=True, blank=True)
    pinned_object_token = models.CharField(max_length=32, null=True, blank=True)
