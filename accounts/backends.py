"""Authenticate the built-in Django user by exact username or unique email."""
from django.contrib.auth import get_user_model
from django.contrib.auth.backends import ModelBackend


class UsernameOrEmailBackend(ModelBackend):
    """Use Django password hashing and active-user checks for both identifiers."""

    def authenticate(self, request, username=None, password=None, **kwargs):
        identifier = username or kwargs.get("email")
        if not identifier or password is None:
            return None

        user_model = get_user_model()
        manager = user_model._default_manager
        username_field = user_model.USERNAME_FIELD
        username_match = manager.filter(**{username_field: identifier}).first()
        email_matches = list(manager.filter(email__iexact=identifier)[:2])
        candidates = {user.pk: user for user in email_matches}
        if username_match is not None:
            candidates[username_match.pk] = username_match

        if len(candidates) != 1:
            # Run the configured password hasher on misses to reduce timing leaks.
            user_model().set_password(password)
            return None

        user = next(iter(candidates.values()))
        if user.check_password(password) and self.user_can_authenticate(user):
            return user
        return None
