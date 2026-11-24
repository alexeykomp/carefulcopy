"""Copy a directory tree with a live progress line; Ctrl+C aborts cleanly.

    python tree.py /data/photos smb://nas.local/Foto/backup/photos
Run it again after an interruption: only what is missing or changed is copied.
"""
import sys
import threading

from carefulcopy import AdaptiveThrottle, TreeOptions, copy_tree

stop = threading.Event()


def show(p):
    bar = "#" * int(p.fraction * 30)
    print(f"\r{p.phase:9} [{bar:<30}] {p.files_done}/{p.files_total} files", end="", flush=True)


if __name__ == "__main__":
    src, dst = sys.argv[1:3]
    try:
        report = copy_tree(src, dst, progress=show, abort=stop, throttle=AdaptiveThrottle(),
                           tree=TreeOptions(workers=4, if_exists="update", errors="collect"))
    except KeyboardInterrupt:
        stop.set()
        raise SystemExit("\naborted")
    print(f"\n{report.files_copied} copied, {report.files_skipped} skipped, {len(report.failures)} failed")
    for f in report.failures:
        print("  FAILED", f.source, "-", f.error)
