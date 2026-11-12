import pytest

from jef.secrets import SecretStore


class MemoryBackend:
    """Stands in for the OS keychain so tests never touch your real one."""

    def __init__(self):
        self.data = {}

    def get_password(self, service, username):
        return self.data.get((service, username))

    def set_password(self, service, username, password):
        self.data[(service, username)] = password

    def delete_password(self, service, username):
        del self.data[(service, username)]


@pytest.fixture
def secret_store():
    return SecretStore(MemoryBackend())
