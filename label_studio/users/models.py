"""This file and its contents are licensed under the Apache License 2.0. Please see the included NOTICE for copyright information and LICENSE for a copy of the license.
"""
import datetime
import uuid
from typing import Optional

from core.feature_flags import flag_set
from core.utils.common import batch, load_func
from core.utils.db import fast_first
from django.conf import settings
from django.contrib import auth
from django.contrib.auth.base_user import AbstractBaseUser, BaseUserManager
from django.contrib.auth.models import PermissionsMixin
from django.contrib.auth.signals import user_logged_in
from django.core.exceptions import EmptyResultSet, PermissionDenied
from django.db import connections, models, router, transaction
from django.db.models.signals import post_save
from django.dispatch import receiver
from django.utils import timezone
from django.utils.crypto import salted_hmac
from django.utils.functional import cached_property
from django.utils.translation import gettext_lazy as _
from organizations.models import Organization
from rest_framework.authtoken.models import Token
from users.functions import hash_upload
from users.functions.last_activity import get_user_last_activity, schedule_activity_sync, set_user_last_activity

YEAR_START = 1980
YEAR_CHOICES = []
for r in range(YEAR_START, (datetime.datetime.now().year + 1)):
    YEAR_CHOICES.append((r, r))

year = models.IntegerField(_('year'), choices=YEAR_CHOICES, default=datetime.datetime.now().year)


class UserQuerySet(models.QuerySet):
    def delete(self):
        from data_export.audit_deletion import coordinated
        with coordinated(self, 'users') as fixed:
            return super(UserQuerySet, fixed).delete()

    def _security_batches(self, objects, *, fields=('is_active',), batch_size=None, reserved_params=0):
        connection = connections[self.db]
        fields = [self.model._meta.get_field(name) for name in fields]
        # Include CASE value/PK pairs and the final ID predicate in the budget.
        # Retain a conservative protocol bound when the backend reports no limit.
        parameter_limit = connection.features.max_query_params or 65535
        available_parameters = parameter_limit - reserved_params
        if available_parameters <= 0:
            raise ValueError('The queryset leaves no parameter capacity for ID discovery.')
        size = min(
            batch_size or 1000,
            1000,
            available_parameters,
            max(1, parameter_limit // (2 * len(fields) + 1)),
            max(1, connection.ops.bulk_batch_size(['pk', 'pk'] + fields, objects)),
        )
        yield from batch(objects, size)

    def _write_locked_account_state(self, targets, values, *, actor, request_id=None):
        from users.session_security import _advance_session_version

        if values['is_active'] is True and any(not was_active for _, was_active in targets):
            raise PermissionDenied('Account reactivation requires the explicit reactivation service.')
        count = 0
        for target_batch in self._security_batches(targets, fields=values):
            # Only the previously locked IDs define scope, even if fields change filters.
            locked = self.model._default_manager.using(self.db).filter(pk__in=[pk for pk, _ in target_batch])
            count += models.QuerySet.update(locked, **values)
            if values['is_active'] is False:
                for user_id, was_active in target_batch:
                    if was_active:
                        _advance_session_version(
                            user_id,
                            reason='account_disabled',
                            actor_id=actor.pk,
                            using=self.db,
                            request_id=request_id,
                        )
        return count

    def update(self, *, session_actor=None, session_request_id=None, **kwargs):
        if 'is_active' not in kwargs:
            return super().update(**kwargs)
        from users.session_security import require_session_administrator

        if not isinstance(kwargs['is_active'], bool):
            raise PermissionDenied('Account state changes require an explicit boolean value.')
        self._for_write = True
        with transaction.atomic(using=self.db):
            actor = None
            if kwargs['is_active'] is False:
                actor = require_session_administrator(session_actor, using=self.db)
            targets = list(dict(self.order_by('pk').select_for_update().values_list('pk', 'is_active')).items())
            if actor is not None:
                actor = require_session_administrator(session_actor, using=self.db)
            if not targets:
                # Keep Django's field validation even when no row is selected.
                empty = self.model._default_manager.using(self.db).filter(pk__in=[])
                return models.QuerySet.update(empty, **kwargs)
            return self._write_locked_account_state(targets, kwargs, actor=actor, request_id=session_request_id)

    def bulk_update(self, objs, fields, batch_size=None, *, session_actor=None, session_request_id=None):
        fields = tuple(fields)
        if 'is_active' not in fields:
            return super().bulk_update(objs, fields, batch_size=batch_size)
        from users.session_security import require_session_administrator

        objs = tuple(objs)
        if batch_size is not None and batch_size <= 0:
            raise ValueError('Batch size must be positive.')
        if any(obj.pk is None for obj in objs):
            raise ValueError('All bulk_update() objects must have a primary key set.')
        if any(not isinstance(obj.is_active, bool) for obj in objs):
            raise PermissionDenied('Account state changes require an explicit boolean value.')
        if len({obj.pk for obj in objs}) != len(objs):
            raise ValueError('Account state batches must not contain duplicate users.')
        # Reuse Django's field validation without issuing a write.
        models.QuerySet.bulk_update(self, (), fields, batch_size=batch_size)
        remaining_fields = [field for field in fields if field != 'is_active']
        if not objs:
            return 0
        self._for_write = True
        with transaction.atomic(using=self.db):
            disables = any(not obj.is_active for obj in objs)
            if disables:
                require_session_administrator(session_actor, using=self.db)
            objects_by_id = {obj.pk: obj for obj in objs}
            targets = []
            try:
                scope = self.values_list('pk', 'is_active').query.get_compiler(using=self.db)
                _, scope_parameters = scope.as_sql()
            except EmptyResultSet:
                return 0
            # Acquire the whole target scope before any field write. Global PK order
            # is independent of caller order and prevents reverse-batch deadlocks.
            parameter_limit = connections[self.db].features.max_query_params or 65535
            if len(scope_parameters) == parameter_limit:
                # An executable scope with no spare binds cannot add an ID predicate.
                # Lock it in the same global order, then intersect in Python. Matching
                # users absent from the supplied objects are locked but never written.
                selected = self.order_by('pk').select_for_update().values_list('pk', 'is_active')
                targets = [(pk, active) for pk, active in dict(selected).items() if pk in objects_by_id]
            else:
                for id_batch in self._security_batches(
                    sorted(objects_by_id), fields=fields, batch_size=batch_size, reserved_params=len(scope_parameters)
                ):
                    selected = self.filter(pk__in=id_batch).order_by('pk').select_for_update()
                    targets.extend(dict(selected.values_list('pk', 'is_active')).items())
            actor = require_session_administrator(session_actor, using=self.db) if disables else None
            for target_batch in self._security_batches(targets, fields=fields, batch_size=batch_size):
                locked = self.model._default_manager.using(self.db).filter(pk__in=[pk for pk, _ in target_batch])
                selected_objects = [objects_by_id[pk] for pk, _ in target_batch]
                if remaining_fields:
                    models.QuerySet.bulk_update(locked, selected_objects, remaining_fields, batch_size=batch_size)
                for active in (False, True):
                    selected = [
                        (pk, was_active) for pk, was_active in target_batch if objects_by_id[pk].is_active is active
                    ]
                    if selected:
                        # Authority is freshly checked once for the whole operation;
                        # disabling its own actor must not change later batch intent.
                        self._write_locked_account_state(
                            selected, {'is_active': active}, actor=actor, request_id=session_request_id
                        )
            return len(targets)


class UserManager(BaseUserManager.from_queryset(UserQuerySet)):
    use_in_migrations = True

    def _create_user(self, email, password, **extra_fields):
        """
        Create and save a user with the given email and password.
        """
        if not email:
            raise ValueError('Must specify an email address')

        email = self.normalize_email(email)
        user = self.model(email=email, **extra_fields)

        user.set_password(password)
        user.save(using=self._db)

        return user

    def create_user(self, email, password=None, **extra_fields):
        extra_fields.setdefault('is_staff', False)
        extra_fields.setdefault('is_superuser', False)
        return self._create_user(email, password, **extra_fields)

    def create_superuser(self, email, password, **extra_fields):
        extra_fields.setdefault('is_staff', True)
        extra_fields.setdefault('is_superuser', True)

        if extra_fields.get('is_staff') is not True:
            raise ValueError('Superuser must have is_staff=True.')
        if extra_fields.get('is_superuser') is not True:
            raise ValueError('Superuser must have is_superuser=True.')

        return self._create_user(email, password, **extra_fields)


class UserLastActivityMixin(models.Model):
    last_activity = models.DateTimeField(_('last activity'), default=timezone.now, editable=False)

    def update_last_activity(self):
        """Update user's last activity timestamp using Redis caching."""
        current_time = timezone.now()

        if flag_set('fflag_fix_back_plt_840_redis_last_activity_29072025_short', user='auto'):
            redis_success = set_user_last_activity(self.id, current_time)

            if not redis_success:
                self.last_activity = current_time
                self.save(update_fields=['last_activity'])
            else:
                schedule_activity_sync()
        else:
            self.last_activity = current_time
            self.save(update_fields=['last_activity'])

    def get_last_activity(self):
        """Get user's last activity timestamp with Redis caching."""
        # Try Redis first, fallback to database
        cached_activity = get_user_last_activity(self.id)

        if cached_activity is not None:
            return cached_activity

        # If not in Redis, return database value
        return self.last_activity

    @property
    def last_activity_cached(self):
        """Property for accessing cached last activity."""
        return self.get_last_activity()

    class Meta:
        abstract = True


UserMixin = load_func(settings.USER_MIXIN)


class User(UserMixin, AbstractBaseUser, PermissionsMixin, UserLastActivityMixin):
    def delete(self, *args, **kwargs):
        from data_export.audit_deletion import coordinated
        with coordinated(User.objects.using(kwargs.get('using') or self._state.db).filter(pk=self.pk), 'users'):
            return super().delete(*args, **kwargs)

    """
    An abstract base class implementing a fully featured User model with
    admin-compliant permissions.

    Username and password are required. Other fields are optional.
    """

    username = models.CharField(_('username'), max_length=256)
    email = models.EmailField(_('email address'), unique=True, blank=True)

    first_name = models.CharField(_('first name'), max_length=256, blank=True)
    last_name = models.CharField(_('last name'), max_length=256, blank=True)
    phone = models.CharField(_('phone'), max_length=256, blank=True)
    avatar = models.ImageField(upload_to=hash_upload, blank=True)
    custom_hotkeys = models.JSONField(
        _('custom hotkeys'),
        default=dict,
        blank=True,
        null=True,
        help_text=_('Custom keyboard shortcuts configuration for the user interface'),
    )

    is_staff = models.BooleanField(
        _('staff status'), default=False, help_text=_('Designates whether the user can log into this admin site.')
    )

    is_active = models.BooleanField(
        _('active'),
        default=True,
        help_text=_('Designates whether to treat this user as active. Unselect this instead of deleting accounts.'),
    )

    date_joined = models.DateTimeField(_('date joined'), default=timezone.now)

    activity_at = models.DateTimeField(_('last annotation activity'), auto_now=True)

    active_organization = models.ForeignKey(
        'organizations.Organization', null=True, on_delete=models.SET_NULL, related_name='active_users'
    )

    allow_newsletters = models.BooleanField(
        _('allow newsletters'), null=True, default=None, help_text=_('Allow sending newsletters to user')
    )

    objects = UserManager()

    EMAIL_FIELD = 'email'
    USERNAME_FIELD = 'email'
    REQUIRED_FIELDS = ()

    def save(self, *args, session_actor=None, session_request_id=None, **kwargs):
        # Provisioning/security signals and account state must commit together.
        using = (
            kwargs.get('using')
            or (args[2] if len(args) > 2 else None)
            or router.db_for_write(type(self), instance=self)
        )
        update_fields = kwargs.get('update_fields', args[3] if len(args) > 3 else None)
        writes_active = update_fields is None or 'is_active' in update_fields
        if writes_active and not isinstance(self.is_active, bool):
            raise PermissionDenied('Account state changes require an explicit boolean value.')
        with transaction.atomic(using=using):
            previous = None
            if self.pk and writes_active:
                previous = type(self).objects.using(using).select_for_update().filter(pk=self.pk).first()
            if previous is not None and not previous.is_active and self.is_active:
                if update_fields is not None:
                    raise PermissionDenied('Account reactivation requires the explicit reactivation service.')
                # Full profile saves preserve a concurrent disable, even for administrators.
                # Only the explicit reactivation service may perform the opposite transition.
                self.is_active = False
            disables = previous is not None and previous.is_active and self.is_active is False
            actor = None
            if disables:
                from users.session_security import require_session_administrator

                actor = require_session_administrator(session_actor, using=using)
            result = super().save(*args, **kwargs)
            if disables:
                from users.session_security import _advance_session_version

                _advance_session_version(
                    self.pk,
                    reason='account_disabled',
                    actor_id=actor.pk,
                    using=using,
                    request_id=session_request_id,
                )
            return result

    def _get_session_auth_hash(self, secret=None):
        # Django checks this on every session-authenticated request, including
        # SECRET_KEY fallback verification. Never use a cached related object.
        using = self._state.db or router.db_for_write(UserSessionVersion)
        try:
            version, boundary = (
                UserSessionVersion.objects.using(using)
                .values_list(
                    'version',
                    'user__session_revocation_boundary__version',
                )
                .get(user_id=self.pk)
            )
        except UserSessionVersion.DoesNotExist:
            # Django treats an empty auth hash as invalid and flushes existing
            # sessions. The login signal below also rejects a new empty hash.
            return ''
        if boundary is None or version != boundary:
            return ''
        password_hash = super()._get_session_auth_hash(secret=secret)
        return salted_hmac(
            'users.User.session_security', f'{password_hash}:{version}', secret=secret, algorithm='sha256'
        ).hexdigest()

    class Meta:
        db_table = 'htx_user'
        verbose_name = _('user')
        verbose_name_plural = _('users')
        indexes = [
            models.Index(fields=['username']),
            models.Index(fields=['email']),
            models.Index(fields=['first_name']),
            models.Index(fields=['last_name']),
            models.Index(fields=['date_joined']),
        ]

    @cached_property
    def avatar_url(self):
        if self.avatar:
            if settings.CLOUD_FILE_STORAGE_ENABLED:
                return self.avatar.url
            else:
                return settings.HOSTNAME + self.avatar.url

    def is_organization_admin(self, org_pk):
        return True

    def active_organization_annotations(self):
        return self.annotations.filter(project__organization=self.active_organization)

    def active_organization_contributed_project_number(self):
        annotations = self.active_organization_annotations()
        return annotations.values_list('project').distinct().count()

    @cached_property
    def own_organization(self) -> Optional[Organization]:
        return fast_first(Organization.objects.filter(created_by=self))

    @cached_property
    def has_organization(self):
        return Organization.objects.filter(created_by=self).exists()

    def clean(self):
        super().clean()
        self.email = self.__class__.objects.normalize_email(self.email)

    def name_or_email(self):
        name = self.get_full_name()
        if len(name) == 0:
            name = self.email

        return name

    def get_full_name(self):
        """
        Return the first_name and the last_name for a given user with a space in between.
        """
        full_name = '%s %s' % (self.first_name, self.last_name)
        return full_name.strip()

    def get_short_name(self):
        """Return the short name for the user."""
        return self.first_name

    def get_token(self) -> Token:
        return Token.objects.filter(user=self).first()

    def reset_token(self) -> Token:
        Token.objects.filter(user=self).delete()
        return Token.objects.create(user=self)

    def get_initials(self, is_deleted=False):
        initials = '?'

        if is_deleted:
            return 'DU'

        if not self.first_name and not self.last_name:
            initials = self.email[0:2]
        elif self.first_name and not self.last_name:
            initials = self.first_name[0:1]
        elif self.last_name and not self.first_name:
            initials = self.last_name[0:1]
        elif self.first_name and self.last_name:
            initials = self.first_name[0:1] + self.last_name[0:1]
        return initials


class UserSessionVersion(models.Model):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, primary_key=True, on_delete=models.CASCADE)
    version = models.PositiveBigIntegerField(default=0, editable=False)

    class Meta:
        db_table = 'htx_user_session_version'


class UserSessionRevocationBoundary(models.Model):
    """Recovery high-water mark, independent of audit delivery and mutable user fields."""

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        primary_key=True,
        on_delete=models.CASCADE,
        related_name='session_revocation_boundary',
    )
    version = models.PositiveBigIntegerField(default=0, editable=False)

    class Meta:
        db_table = 'htx_user_session_revocation_boundary'


class SessionRevocationEvent(models.Model):
    """The local durable audit receiver. Event and security transition commit together."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    event_type = models.CharField(max_length=32, default='browser_session_revoked', editable=False)
    revocation_type = models.CharField(max_length=16, default='user', editable=False)
    actor_type = models.CharField(max_length=16, default='human', editable=False)
    actor_user_id = models.PositiveBigIntegerField(editable=False)
    target_user_id = models.PositiveBigIntegerField(db_index=True, editable=False)
    reason_code = models.CharField(max_length=32, editable=False)
    security_version = models.PositiveBigIntegerField(editable=False)
    occurred_at = models.DateTimeField(default=timezone.now, editable=False)
    correlation_id = models.UUIDField(default=uuid.uuid4, editable=False)

    class Meta:
        db_table = 'htx_session_revocation_event'
        constraints = [
            models.UniqueConstraint(
                fields=['target_user_id', 'security_version'],
                name='unique_session_revocation_event',
            )
        ]


class UserLocalePreference(models.Model):
    """Optional display preference, deliberately separate from authentication state."""

    user = models.OneToOneField(settings.AUTH_USER_MODEL, primary_key=True, on_delete=models.CASCADE)
    locale = models.CharField(max_length=5, null=True, blank=True, choices=(('en-US', 'English'), ('zh-CN', '简体中文')))

    class Meta:
        db_table = 'htx_user_locale_preference'


@receiver(user_logged_in, sender=User)
def reject_unversioned_login(sender, request, user, **kwargs):
    if not request.session.get(auth.HASH_SESSION_KEY):
        auth.logout(request)
        raise PermissionDenied('User session security state is missing.')


@receiver(post_save, sender=User)
def init_user(sender, instance=None, created=False, raw=False, using=None, update_fields=None, **kwargs):
    if raw:
        return
    if created:
        UserSessionVersion.objects.using(using).create(user=instance)
        UserSessionRevocationBoundary.objects.using(using).create(user=instance)
        # create token for user
        Token.objects.using(using).create(user=instance)
