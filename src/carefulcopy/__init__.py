"""carefulcopy - resumable, atomic file copying with pluggable backends."""
from .abort import is_aborted
from .backends import (
    Backend, FileInfo, LocalBackend, configure_smb, get_backend, register_backend, reset_backends,
    unregister_backend,
)
from .backends.smb import SMBBackend
from .credentials import (
    CallbackCredentials, ChainCredentials, Credentials, EnvCredentials, KeyringCredentials,
    PromptCredentials, StaticCredentials,
)
from .pool import ConnectionPool
from .retry import BatchedFailurePrompt
from .copier import copy_file
from .errors import (
    AuthenticationError, CopyError, LockedError, SourceChangedError, TransientError, UnsupportedSchemeError,
)
from .location import Location
from .options import CopyOptions
from .throttle import AdaptiveThrottle, FixedRateThrottle, Speeds, Throttle, ThrottleWorker, TokenBucket

__version__ = "0.1.0.dev0"

__all__ = [
    "copy_file", "CopyOptions", "Location",
    "Throttle", "ThrottleWorker", "AdaptiveThrottle", "FixedRateThrottle", "TokenBucket", "Speeds",
    "Backend", "FileInfo", "LocalBackend", "SMBBackend", "ConnectionPool",
    "register_backend", "unregister_backend", "get_backend", "configure_smb", "reset_backends", "is_aborted",
    "Credentials", "StaticCredentials", "EnvCredentials", "CallbackCredentials", "PromptCredentials",
    "KeyringCredentials", "ChainCredentials", "BatchedFailurePrompt", "AuthenticationError",
    "CopyError", "LockedError", "SourceChangedError", "TransientError", "UnsupportedSchemeError",
]
