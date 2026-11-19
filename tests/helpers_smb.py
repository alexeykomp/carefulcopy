from carefulcopy import SMBBackend, StaticCredentials, Location, register_backend, reset_backends

from fake_smb import FakeServer
from helpers import Base


class SMBCase(Base):
    """A fake SMB server at smb://nas/share, wired to a real SMBBackend."""

    def setUp(self):
        super().setUp()
        self.server = FakeServer()
        self.backend = self.make_backend()
        self.addCleanup(reset_backends)
        self.addCleanup(self.backend.close)
        register_backend("smb", lambda loc: self.backend)

    def make_backend(self, credentials=None, **kw):
        kw.setdefault("pool_size", 4)
        return SMBBackend(credentials or StaticCredentials("alice", "secret"),
                          smbclient_module=self.server.client, auto_reap=False, **kw)

    @staticmethod
    def uri(rel, host="nas", share="share"):
        return f"smb://{host}/{share}/{rel}"

    def loc(self, rel):
        return Location.parse(self.uri(rel))
