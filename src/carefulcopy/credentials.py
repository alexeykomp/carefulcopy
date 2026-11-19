"""Where SMB (and future backends) get their login from.

A *provider* is any object with ``get(host) -> Optional[Credentials]``.
It may also implement two optional hooks:

* ``remember(host, creds)`` - called once after the credentials were accepted
  by the server (e.g. to store them in the OS keychain);
* ``invalidate(host)`` - called when the server rejected them.

Returning ``None`` from ``get`` means "no explicit login": the SMB library then
uses ambient authentication (e.g. a Kerberos ticket).
"""
from __future__ import annotations

import getpass
import json
import logging
import os
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Optional, Protocol

from .errors import AuthenticationError

logger = logging.getLogger("carefulcopy.credentials")


@dataclass(frozen=True)
class Credentials:
    username: str
    password: str = field(repr=False)


class CredentialProvider(Protocol):
    def get(self, host: str) -> Optional[Credentials]: ...


class StaticCredentials:
    """Fixed login, optionally with per-host overrides."""

    def __init__(self, username: Optional[str] = None, password: Optional[str] = None,
                 per_host: Optional[Mapping[str, Credentials]] = None) -> None:
        self._default = Credentials(username, password or "") if username is not None else None
        self._per_host = dict(per_host or {})

    def get(self, host: str) -> Optional[Credentials]:
        return self._per_host.get(host, self._default)


class EnvCredentials:
    """Read ``<PREFIX>_USERNAME`` / ``<PREFIX>_PASSWORD`` from the environment."""

    def __init__(self, prefix: str = "CAREFULCOPY_SMB", environ: Optional[Mapping[str, str]] = None) -> None:
        self._prefix = prefix
        self._environ = os.environ if environ is None else environ

    def get(self, host: str) -> Optional[Credentials]:
        user = self._environ.get(f"{self._prefix}_USERNAME")
        if not user:
            return None
        return Credentials(user, self._environ.get(f"{self._prefix}_PASSWORD", ""))


class CallbackCredentials:
    """Delegate to a function ``fn(host) -> Credentials | None`` (e.g. a GUI dialog).

    Calls are serialised, so concurrent copy threads never open two dialogs
    for the same host: the second caller simply waits and gets the same answer
    from the backend's cache.
    """

    def __init__(self, fn: Callable[[str], Optional[Credentials]]) -> None:
        self._fn = fn
        self._lock = threading.Lock()

    def get(self, host: str) -> Optional[Credentials]:
        with self._lock:
            return self._fn(host)


class PromptCredentials:
    """Ask on the console (``input`` + ``getpass``)."""

    def __init__(self, default_username: str = "",
                 input_fn: Callable[[str], str] = input,
                 getpass_fn: Callable[[str], str] = getpass.getpass) -> None:
        self._default, self._input, self._getpass = default_username, input_fn, getpass_fn
        self._lock = threading.Lock()

    def get(self, host: str) -> Optional[Credentials]:
        with self._lock:
            try:
                user = self._input(f"[{host}] username [{self._default}]: ").strip() or self._default
                password = self._getpass(f"[{host}] password: ")
            except (EOFError, KeyboardInterrupt) as exc:
                raise AuthenticationError(f"Credential input for {host} was cancelled") from exc
        if not user or not password:
            raise AuthenticationError(f"Empty credentials for {host}")
        return Credentials(user, password)


class KeyringCredentials:
    """Look the login up in the OS keychain (needs ``pip install keyring``) and
    save it there once the server has accepted it. *fallback* is asked when
    nothing is stored (typically a prompt)."""

    def __init__(self, fallback: Optional[CredentialProvider] = None, service: str = "carefulcopy",
                 keyring_module: Any = None) -> None:
        self._fallback, self.service, self._kr = fallback, service, keyring_module

    def _keyring(self) -> Any:
        if self._kr is None:
            import keyring  # raises ImportError with the usual message
            self._kr = keyring
        return self._kr

    def get(self, host: str) -> Optional[Credentials]:
        try:
            raw = self._keyring().get_password(self.service, host)
            if raw:
                data = json.loads(raw)
                return Credentials(data["username"], data["password"])
        except ImportError:
            raise
        except Exception as exc:
            logger.warning("keyring lookup for %s failed: %s", host, exc)
        return self._fallback.get(host) if self._fallback else None

    def remember(self, host: str, creds: Optional[Credentials]) -> None:
        if creds is None:
            return
        try:
            kr = self._keyring()
            payload = json.dumps({"username": creds.username, "password": creds.password})
            if kr.get_password(self.service, host) != payload:
                kr.set_password(self.service, host, payload)
        except Exception as exc:
            logger.warning("could not save credentials for %s to the keyring: %s", host, exc)
        if self._fallback is not None and hasattr(self._fallback, "remember"):
            self._fallback.remember(host, creds)

    def invalidate(self, host: str) -> None:
        try:
            self._keyring().delete_password(self.service, host)
        except Exception:
            pass
        if self._fallback is not None and hasattr(self._fallback, "invalidate"):
            self._fallback.invalidate(host)


class ChainCredentials:
    """Try providers in order; the first one that returns credentials wins."""

    def __init__(self, *providers: CredentialProvider) -> None:
        self._providers = providers

    def get(self, host: str) -> Optional[Credentials]:
        for p in self._providers:
            creds = p.get(host)
            if creds is not None:
                return creds
        return None

    def remember(self, host: str, creds: Optional[Credentials]) -> None:
        for p in self._providers:
            if hasattr(p, "remember"):
                p.remember(host, creds)

    def invalidate(self, host: str) -> None:
        for p in self._providers:
            if hasattr(p, "invalidate"):
                p.invalidate(host)
