"""Shared identity and navigation context for authenticated application pages."""


def app_page_context(request, *, page_title, active_navigation, extra=None):
    membership = request.workspace_membership
    user = request.user
    identity_name = user.get_full_name().strip() or user.get_username()
    context = {
        "app_name": "BrandGuard",
        "page_title": page_title,
        "active_navigation": active_navigation,
        "membership": membership,
        "workspace": membership.workspace,
        "identity_name": identity_name,
        "user_email": user.email or user.get_username(),
    }
    if extra:
        context.update(extra)
    return context
