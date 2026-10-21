from types import SimpleNamespace

from django.contrib.auth.models import AnonymousUser
from django.test import TestCase
from rest_framework.exceptions import AuthenticationFailed, NotAuthenticated, PermissionDenied
from users.tests.factories import UserFactory

from access_control.authorization import AuthorizationService
from access_control.identity import LOCAL_IDENTITY_SOURCE, LocalIdentityProvider, Principal, get_identity_provider, resolve_actor


class TestLocalIdentityProvider(TestCase):
    def setUp(self):
        self.provider = LocalIdentityProvider()

    def test_resolves_distinct_local_users_to_distinct_principals(self):
        first = UserFactory()
        second = UserFactory()

        first_principal = self.provider.resolve(SimpleNamespace(user=first))
        second_principal = self.provider.resolve(SimpleNamespace(user=second))

        assert first_principal.source == LOCAL_IDENTITY_SOURCE
        assert first_principal.local_user_id == first.id
        assert first_principal.principal_id == f"{LOCAL_IDENTITY_SOURCE}:{first.id}"
        assert second_principal.local_user_id == second.id
        assert first_principal.principal_id != second_principal.principal_id

    def test_rejects_anonymous_user(self):
        with self.assertRaises(NotAuthenticated):
            self.provider.resolve(SimpleNamespace(user=AnonymousUser()))

    def test_authorization_require_denies_false_decisions(self):
        authorization = AuthorizationService()

        with self.assertRaises(PermissionDenied):
            authorization.require(False)

    def test_resolve_actor_rejects_unmapped_principal(self):
        class UnmappedProvider:
            def resolve(self, request):
                return Principal(
                    principal_id="external:missing",
                    source="external",
                    username="missing@example.com",
                    local_user_id=None,
                )

        with self.assertRaises(AuthenticationFailed):
            resolve_actor(SimpleNamespace(user=UserFactory()), provider=UnmappedProvider())

    def test_configured_provider_defaults_to_local_provider(self):
        get_identity_provider.cache_clear()
        provider = get_identity_provider()
        assert isinstance(provider, LocalIdentityProvider)


class TestProjectAuthorityScope(TestCase):
    def setUp(self):
        from organizations.tests.factories import OrganizationFactory
        from projects.models import ProjectMember
        from projects.tests.factories import ProjectFactory

        self.organization = OrganizationFactory()
        self.project = ProjectFactory(organization=self.organization)
        self.user = UserFactory(active_organization=self.organization)
        ProjectMember.objects.create(project=self.project, user=self.user, role='manager')
        self.principal = LocalIdentityProvider().resolve(SimpleNamespace(user=self.user))

    def test_owner_requires_live_organization_membership(self):
        from organizations.models import OrganizationMember

        owner = self.organization.created_by
        principal = LocalIdentityProvider().resolve(SimpleNamespace(user=owner))
        assert AuthorizationService.can_manage_project(principal, self.project)
        OrganizationMember.objects.filter(user=owner, organization=self.organization).delete()
        assert not AuthorizationService.can_manage_project(principal, self.project)

    def test_residual_project_role_cannot_replace_organization_membership(self):
        from organizations.models import OrganizationMember

        assert AuthorizationService.can_manage_project(self.principal, self.project)
        OrganizationMember.objects.get(user=self.user, organization=self.organization).soft_delete()
        assert AuthorizationService.project_role(self.principal, self.project) is None

    def test_foreign_organization_cannot_gain_authority_from_invalid_role_row(self):
        from organizations.models import OrganizationMember
        from organizations.tests.factories import OrganizationFactory

        OrganizationMember.objects.filter(user=self.user, organization=self.organization).delete()
        OrganizationMember.objects.create(user=self.user, organization=OrganizationFactory())
        assert not AuthorizationService.can_manage_project(self.principal, self.project)

    def test_cached_principal_does_not_keep_disabled_account_authority(self):
        from users.models import User

        administrator = UserFactory(is_staff=True, is_superuser=True)
        User.objects.filter(pk=self.user.pk).update(is_active=False, session_actor=administrator)
        assert AuthorizationService.project_role(self.principal, self.project) is None

    def test_switching_selected_organization_does_not_revoke_actual_membership(self):
        from organizations.models import OrganizationMember
        from organizations.tests.factories import OrganizationFactory

        self.user.active_organization = OrganizationFactory()
        OrganizationMember.objects.create(user=self.user, organization=self.user.active_organization)
        self.user.save(update_fields=['active_organization'])
        assert AuthorizationService.can_manage_project(self.principal, self.project)

    def test_missing_organization_scope_defaults_to_deny(self):
        project = SimpleNamespace(organization_id=None, created_by_id=self.user.pk)
        assert AuthorizationService.project_role(self.principal, project) is None
