"""Fail-closed storage used when production has no private object store."""
from django.core.exceptions import ImproperlyConfigured
from django.core.files.storage import Storage


class DisabledPrivateDocumentStorage(Storage):
    """Prevent any accidental upload to Render's ephemeral local filesystem."""

    error_message = (
        "Private document imports are disabled because persistent private storage is not configured."
    )

    def _open(self, name, mode="rb"):
        raise ImproperlyConfigured(self.error_message)

    def _save(self, name, content):
        raise ImproperlyConfigured(self.error_message)

    def exists(self, name):
        return False

    def delete(self, name):
        raise ImproperlyConfigured(self.error_message)

