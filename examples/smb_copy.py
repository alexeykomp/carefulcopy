"""Copy to/from a NAS, asking for the password on the console the first time.

    python smb_copy.py /data/big.iso smb://nas.local/Foto/backup/big.iso
"""
import sys

from carefulcopy import (AdaptiveThrottle, ChainCredentials, EnvCredentials, PromptCredentials,
                         configure_smb, copy_file)

if __name__ == "__main__":
    src, dst = "D:\\PSAutoRecover\\a.jpg", "smb://192.168.178.1/FRITZ.NAS/DATABASE/Download/123.jpg"

    backend = configure_smb(ChainCredentials(EnvCredentials(), PromptCredentials()))
    try:
        ok = copy_file(src, dst, throttle=AdaptiveThrottle(),
                       progress=lambda s, done, total: print(f"\r{100 * done // max(total, 1):3d}%", end="", flush=True))
        print("\ndone" if ok else "\naborted")
    finally:
        backend.close()
