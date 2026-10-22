from functools import wraps

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied

from .models import WorkspaceMembership


def resolve_current_membership(user):
    """Return the user's sole active workspace membership, or None if ambiguous."""
    if not user.is_authenticated or not user.is_active:
        return None
    memberships = list(
        WorkspaceMembership.objects.filter(
            user=user,
            is_active=True,
            workspace__is_active=True,
        )
        .select_related("workspace")[:2]
    )
    return memberships[0] if len(memberships) == 1 else None


def workspace_member_required(view_func):
    """Resolve workspace scope from the authenticated user, never client input."""

    @login_required
    @wraps(view_func)
    def wrapped(request, *args, **kwargs):
        membership = resolve_current_membership(request.user)
        if membership is None:
            raise PermissionDenied("An unambiguous active workspace membership is required.")
        request.workspace_membership = membership
        request.workspace = membership.workspace
        return view_func(request, *args, **kwargs)

    return wrapped


def workspace_role_required(*allowed_roles):
    """Protect a view using the role on the resolved workspace membership."""
    allowed = set(allowed_roles)

    def decorator(view_func):
        @workspace_member_required
        @wraps(view_func)
        def wrapped(request, *args, **kwargs):
            if request.workspace_membership.role not in allowed:
                raise PermissionDenied("Your workspace role cannot perform this action.")
            return view_func(request, *args, **kwargs)

        return wrapped

    return decorator
