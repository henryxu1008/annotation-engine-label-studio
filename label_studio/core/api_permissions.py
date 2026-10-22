from access_control.identity import resolve_actor
from rest_framework.permissions import SAFE_METHODS, BasePermission


class HasObjectPermission(BasePermission):
    def has_object_permission(self, request, view, obj):
        return obj.has_permission(request.user)


class HasActorObjectPermission(HasObjectPermission):
    """Opt in only after a view's collection scope uses the trusted actor too."""

    def has_object_permission(self, request, view, obj):
        # Keep object checks on the same trusted actor as query scoping and
        # persisted audit fields, without replacing the authenticated session.
        _, actor = resolve_actor(request)
        return obj.has_permission(actor)


class MemberHasOwnerPermission(BasePermission):
    def has_object_permission(self, request, view, obj):
        if request.method not in SAFE_METHODS and not request.user.own_organization:
            return False

        return obj.has_permission(request.user)
