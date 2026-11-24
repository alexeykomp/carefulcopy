# carefulcopy - the only copying engine you need

Reliable file copying for flaky storage: **atomic**, **resumable**, with **retries**,
**destination locks** and pluggable **backends** (local today, SMB next).

> Status: early development (0.1.0.dev). The copy engine, local backend,
> bandwidth throttling and the SMB backend are written and tested.

## Why

`shutil.copyfile` leaves a truncated file behind if the connection drops, and
starts from zero on retry. carefulcopy:

* writes to `<name>.part.<size>-<mtime_ns>-<hash of the source location>` and renames only when complete and verified;
* **resumes** from the last byte written after a transient error (reset, timeout, ...);
* detects a **source modified during the copy** and refuses to produce a mixed file;
* takes an exclusive **lock** on the destination so two writers cannot collide, and
  cleans up stale `.part` files of older source versions;
* supports clean **abort** (`threading.Event`, bool, callable) at block granularity;
* can **throttle** bandwidth - a fixed cap or an adaptive limit that learns the link speed;
* has a tiny **Backend** interface, so the same engine works on any storage.

## Usage

```python
import threading
from carefulcopy import copy_file, CopyOptions

stop = threading.Event()

ok = copy_file(
    "/data/big.iso", "/backup/big.iso",
    progress=lambda src, done, total: print(f"{done}/{total}"),
    abort=stop,
    options=CopyOptions(block_size=4 * 1024 * 1024, retries=5),
)
# ok is False if `stop` fired; errors raise CopyError / OSError subclasses
```

Locations are plain paths or URIs such as `smb://host/share/dir/file`. URI
paths are used verbatim; there is no percent-decoding.

## Bandwidth throttling

Create one throttle and share it between all copies (and threads) that should
compete for the same bandwidth:

```python
from carefulcopy import copy_file, AdaptiveThrottle, FixedRateThrottle

throttle = AdaptiveThrottle()                 # use ~85 % of what the link can do
# throttle = FixedRateThrottle(20 * 1024**2)  # or: never exceed 20 MiB/s

copy_file("/data/a.iso", "smb://nas/share/a.iso", throttle=throttle)  # any thread

throttle.speeds()        # Speeds(download=..., upload=...) in bytes/s, for a UI
```

`AdaptiveThrottle` starts unlimited ("learning"), measures pure network time
every `CopyOptions.feedback_after` bytes, estimates the channel capacity,
smooths it over the last 8 estimates and caps the channel at `headroom` (85 %)
of that. Transfers between the same two endpoints share one token bucket.
Measurements older than 60 s are discarded and the channel goes back to
learning. Local-to-local copies are never throttled by it.

Notes: with several parallel transfers the estimate is an approximation (each
worker's measured speed is scaled by the number of workers), so the effective
headroom can differ somewhat from the nominal one. Samples are only taken
every 8 MiB by default, so many tiny files rely on what was learned earlier.
Pass `max_rate=` for a hard ceiling on top of the adaptive limit.

## SMB

```
pip install 'carefulcopy[smb]'          # pulls in smbprotocol; add [keyring] for the OS keychain
```

```python
from carefulcopy import (copy_file, configure_smb, ChainCredentials, KeyringCredentials,
                         PromptCredentials, BatchedFailurePrompt)

# Optional - without it, credentials come from CAREFULCOPY_SMB_USERNAME/_PASSWORD
# or from ambient auth (e.g. Kerberos):
backend = configure_smb(
    ChainCredentials(KeyringCredentials(fallback=PromptCredentials("alice"))),
    pool_size=8,
)

copy_file("/data/a.iso", "smb://nas.local/Foto/backup/a.iso")
copy_file("smb://nas.local/Foto/backup/a.iso", "/tmp/a.iso")
```

* **Connections** are pooled (lazy, LIFO, closed after 30 s idle). Each copy attempt
  uses exactly one connection for lock, `.part` file and metadata, so a small pool can
  never deadlock; after a transient error that connection is destroyed, not reused.
* **Credentials** are asked once per host (concurrent threads wait for the first
  answer) and handed to your provider's optional `remember` hook after the server
  accepted them, or `invalidate` hook if it did not (`AuthenticationError`, never retried).
  Providers: `StaticCredentials`, `EnvCredentials`, `CallbackCredentials` (your GUI),
  `PromptCredentials` (console), `KeyringCredentials`, `ChainCredentials`.
* **Errors** are classified by NT status code (e.g. `STATUS_PIPE_BROKEN`,
  `STATUS_NETWORK_NAME_DELETED` => resume; `STATUS_ACCESS_DENIED` => fail).
* **Locks** use a `<name>.lock` sidecar opened so that other writers get
  `STATUS_SHARING_VIOLATION`; your existing destination is never truncated by locking.
  See "Stale locks after a dropped connection" below.
* **Names**: remote path components are normalised to Unicode NFC (macOS sends NFD);
  `sanitize_names=True` replaces characters Windows forbids.
* `backend.check_connection(uri)` is a "Test connection" helper;
  `backend.pause_new_connections()` / `resume_new_connections()` hold back new
  connections, e.g. while a dialog is open.

### Stale locks after a dropped connection

If the link dies silently (Wi-Fi roam, unplugged cable, sleeping laptop) the server keeps the
dead connection's open handles - including our lock - until its own timeout. The retry then meets
a lock held by "someone". Who is it?

The lock file holds a small JSON token (owner id, process id, host, pid). When the lock is busy:

| holder's token | meaning | action |
|---|---|---|
| same process, owner = this copy (or a copy of this process that is no longer running) | our own leftover | **take over**: rename the stale file away, create a fresh lock |
| same process, owner still running | a parallel copy in another thread | wait, never break it |
| other process / machine, or unreadable | someone else - **or a previous run of this application**, which cannot be told apart | wait up to `lock_wait_timeout`, then `CopyError("... held by <host> (pid N)")` |

The token only *identifies*; mutual exclusion is still the share mode of the open handle.
After an application restart the previous run's leftover looks foreign and is waited out
until the server drops it.

### When may a partial file be resumed?

The partial file is `<name>.part.<size>-<mtime_ns>-<hash of the source location>`. A copy resumes
only if **all** hold:

1. it holds the destination lock (with `use_locks=False` nothing prevents two writers);
2. the name matches: same source location, size and exact modification time - so a partial file of
   a *different* source never matches, and neither does the same file reached through another path
   (it starts over; the stale partial file is removed);
3. the last `resume_verify_bytes` (default: one block) of the partial file are identical to the
   same range of the source, which catches a torn or foreign tail. Otherwise the copy restarts from
   zero. `CopyOptions(resume_verify="none")` skips this check.

Bytes before that window are trusted; a damaged middle is not detected. Who wrote the partial file
does not matter - resuming after a crash of the application is a feature.

### One dialog for many failures

When a link drops, every parallel copy fails at once. `BatchedFailurePrompt`
gathers them into a single question:

```python
prompt = BatchedFailurePrompt(
    ask=lambda files: ask_user_to_retry(files),          # runs on a helper thread
    before=backend.pause_new_connections, after=backend.resume_new_connections)
copy_file(src, dst, on_failure=prompt)                   # same object for every copy
```

### Testing against a real server

The SMB tests in the normal suite use `tests/fake_smb.py`, an in-memory imitation of
`smbclient`. It encodes **our understanding** of the library, so passing them does not
prove that understanding right. These points were written from memory and need
confirming against smbprotocol and a real server:

* the `smbclient` calls and keyword arguments used (`open_file(share_access=...)`,
  `replace`, `utime(times=...)`, `register_session(connection_timeout=...)`,
  `reset_connection_cache(connection_cache=...)`), and `truncate()` on `r+b` files;
* the exception attributes carrying the NT status (`ntstatus` / `status`), the status
  values in `backends/smb.py::_STATUS`, and the `"credits are available"` message
  marker (taken from the original project);
* what `share_access=None` means (the lock is written to work with either default).

`tests/test_real_smb.py` checks exactly these. Point it at a scratch folder:

```
export CAREFULCOPY_TEST_SMB_URI='smb://nas.local/share/carefulcopy-tests'
export CAREFULCOPY_TEST_SMB_USER=alice CAREFULCOPY_TEST_SMB_PASSWORD=secret
cd tests && PYTHONPATH=../src python -m unittest test_real_smb -v
```


## Trees and batches

```python
from carefulcopy import copy_tree, copy_many, TreeOptions, CopyOptions, AdaptiveThrottle

report = copy_tree(
    "/data/photos", "smb://nas.local/Foto/backup/photos",     # dst is the copy itself (like shutil.copytree)
    tree=TreeOptions(workers=4, if_exists="update", exclude=["*.tmp", ".DS_Store"]),
    throttle=AdaptiveThrottle(),                                 # shared by all workers
    progress=lambda p: print(f"{p.phase:9} {p.files_done}/{p.files_total} files, {p.fraction:.0%}"),
)
print(report.files_copied, "copied,", report.files_skipped, "skipped;", "OK" if report else "not complete")

copy_many(["/a/one.iso", "/a/dir", "smb://nas.local/Foto/x.bin"], "/backup")   # each keeps its own name
```

* **Plan first.** The source is walked with one directory listing per directory (sizes and mtimes
  included, so no per-file `stat` over SMB), giving exact totals. Progress is reported while scanning too.
* **Aggregated progress.** One `TreeProgress` stream (`phase`, `bytes_copied/total`, `files_done/total`,
  `fraction`, `current` = files in flight) from all workers. Callbacks are **serialised** (never
  concurrent), rate-limited by `progress_interval` (the first and last always arrive) and run on a
  worker thread when `workers > 1`. If your callback raises, the run stops and the error is re-raised.
  Totals exclude skipped files. After a resume or restart of a file the byte count can step back, honestly.
* **`workers=N`** copies N files at once with one throttle and one connection pool (keep N <= `pool_size`).
  Worth it for many small files over a network; leave at 1 for a single big file.
* **`if_exists`**: `overwrite` (default), `skip`, `error`, or `update` = skip when size and mtime match
  (within `mtime_tolerance`, 2 s). **Re-running with `update` after an interruption copies only what is
  missing or changed and resumes partial files** - big copies become restartable. (Needs
  `preserve_mtime=True`, the default.)
* **`errors`**: `"raise"` (default) stops at the first failed file and raises *its* error;
  `"collect"` carries on and lists failures in the report. Authentication errors are always fatal.
  A missing source or copying a directory into itself raises immediately.
* **Report**: `CopyReport(files_copied, bytes_copied, skipped[(source, reason)], failures[(source, error)],
  aborted)`; `bool(report)` is true when everything planned was copied. Aborting returns normally with
  `aborted=True` and leaves only complete files.
* **Filters**: `exclude` patterns (name, or relative path if it contains `/`); carefulcopy's own
  leftovers (`*.part.*`, `x.lock` next to `x`) are skipped so a backup destination can be used as a source;
  symlinks are skipped by default (`follow_symlinks=True`, **local sources only**, with loop detection);
  special files (sockets, devices) are skipped; `max_depth` guards against runaway nesting.
* Not preserved: directory mtimes, permissions/ACLs, hard links. Empty directories are created.

## Writing a backend

Subclass `carefulcopy.Backend` (stat, listdir, open_read, open_write(offset),
makedirs, remove, rename, set_mtime, lock) and register it:

```python
from carefulcopy import register_backend
register_backend("myfs", lambda loc: MyBackend(loc.netloc))
```

Override `is_transient(exc)` to tell the engine which errors mean
"connection problem, resume me" versus "give up".

## Behaviour notes

* `copy_file` returns `False` only when aborted. If retries run out (or your
  `on_failure` callback declines), `CopyError` is raised with the last error as `__cause__`.
* The destination lock is a `<name>.lock` sidecar using OS advisory locks, so a
  crashed process never leaves a stale lock. Windows support is implemented via
  `msvcrt` but not yet covered by CI.
* Resuming trusts the existing `.part` prefix and checks only size and the
  source fingerprint (size + mtime). Hash verification is not implemented.

## Roadmap

1. ~~Engine, local backend, tests~~
2. ~~Bandwidth throttling (adaptive + fixed rate)~~
3. ~~SMB backend, connection pool, credential providers, batched retry prompt~~ (verify on a real server!)
4. ~~`copy_tree` / `copy_many` with aggregated progress~~
5. Examples (Qt retry dialog, CLI), CI, license (not chosen yet), publishing

## Development

```
cd tests && PYTHONPATH=../src python -m unittest discover -v    # or: pytest   (229 tests, ~8 s)

The suite has a 120 s watchdog (`faulthandler`) that dumps all stacks and exits if a
test ever hangs. Throttle tests use a virtual clock, so they run in milliseconds.
```
