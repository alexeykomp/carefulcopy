"""Minimal example: copy a file with progress and Ctrl+C-friendly abort."""
import sys
import threading

from carefulcopy import CopyOptions, copy_file

stop = threading.Event()


def show(src, done, total):
    pct = 100 * done // total if total else 100
    print(f"\r{pct:3d}%  {done}/{total} bytes", end="", flush=True)


if __name__ == "__main__":
    src, dst = sys.argv[1:3]
    try:
        ok = copy_file(src, dst, progress=show, abort=stop, options=CopyOptions())
    except KeyboardInterrupt:
        stop.set()
        ok = False
    print("\ndone" if ok else "\naborted")
