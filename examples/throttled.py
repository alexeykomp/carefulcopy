"""Copy several files in parallel under one shared adaptive throttle."""
import sys
import threading

from carefulcopy import AdaptiveThrottle, copy_file

throttle = AdaptiveThrottle()           # shared => the files compete fairly for the link


def worker(src, dst):
    copy_file(src, dst, throttle=throttle)


if __name__ == "__main__":
    pairs = list(zip(sys.argv[1::2], sys.argv[2::2]))   # src1 dst1 src2 dst2 ...
    threads = [threading.Thread(target=worker, args=p) for p in pairs]
    for t in threads:
        t.start()
    while any(t.is_alive() for t in threads):
        sp = throttle.speeds()
        print(f"\r down {sp.download / 2**20:6.1f} MiB/s   up {sp.upload / 2**20:6.1f} MiB/s", end="", flush=True)
        threads[0].join(0.5)
    print()
