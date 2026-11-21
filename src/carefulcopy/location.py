"""Explicit, backend-independent description of where a file lives."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path, PurePath, PurePosixPath
from typing import Union

# scheme must be >= 2 chars so that Windows drive letters ("C://x") are not
# mistaken for a URI scheme.
_URI_RE = re.compile(r"^([A-Za-z][A-Za-z0-9+.\-]+)://([^/]*)(/.*)?$")
_WIN_DRIVE_RE = re.compile(r"^/[A-Za-z]:")

PathOrLocation = Union[str, "os.PathLike[str]", "Location"]


@dataclass(frozen=True)
class Location:
    """A file or directory, addressed by scheme, host and path.

    ``Location.parse`` accepts plain paths (``/tmp/a.txt``, ``C:\\a.txt``,
    ``pathlib.Path``) and URIs such as ``smb://nas.local/share/dir/a.txt``.
    URI paths are used verbatim: there is **no** percent-decoding, so spaces
    and ``%`` in names work as typed.
    """

    scheme: str
    netloc: str
    path: PurePath

    # -- construction -----------------------------------------------------
    @classmethod
    def local(cls, path: "str | os.PathLike[str]") -> "Location":
        return cls("file", "", Path(path))

    @classmethod
    def parse(cls, value: PathOrLocation) -> "Location":
        if isinstance(value, Location):
            return value
        if not isinstance(value, str):
            return cls.local(os.fspath(value))
        m = _URI_RE.match(value)
        if not m:
            return cls.local(value)
        scheme, netloc, raw_path = m.group(1).lower(), m.group(2), m.group(3) or "/"
        if scheme == "file":
            if netloc not in ("", "localhost"):
                raise ValueError(f"file:// URIs with a host are not supported: {value!r}")
            if _WIN_DRIVE_RE.match(raw_path):
                raw_path = raw_path[1:]
            return cls.local(raw_path)
        return cls(scheme, netloc, PurePosixPath(raw_path))

    # -- path-like helpers ------------------------------------------------
    @property
    def name(self) -> str:
        return self.path.name

    @property
    def parent(self) -> "Location":
        return Location(self.scheme, self.netloc, self.path.parent)

    @property
    def is_local(self) -> bool:
        return self.scheme == "file"

    def with_name(self, name: str) -> "Location":
        return Location(self.scheme, self.netloc, self.path.with_name(name))

    def joinpath(self, *parts: str) -> "Location":
        return Location(self.scheme, self.netloc, self.path.joinpath(*parts))

    def __truediv__(self, part: str) -> "Location":
        return self.joinpath(part)

    def __str__(self) -> str:
        if self.is_local:
            return str(self.path)
        return f"{self.scheme}://{self.netloc}{self.path.as_posix()}"
