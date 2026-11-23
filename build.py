#!/usr/bin/env python3
"""Build dist/TriggerComplete.oxt and optionally install it with unopkg.

The extension reads settings.json and SYSTEM.md from the directory passed via
--config-dir (default: this directory). The TRIGGERCOMPLETE_DIR environment
variable overrides it at runtime.
"""

import argparse
import os
import subprocess
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "extension")


def build(config_dir, out):
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for root, dirs, files in os.walk(SRC):
            dirs[:] = [d for d in dirs if d != "__pycache__"]
            for name in files:
                path = os.path.join(root, name)
                z.write(path, os.path.relpath(path, SRC))
        z.writestr("config_dir.txt", config_dir + "\n")
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config-dir", default=HERE, help="where settings.json and SYSTEM.md live")
    ap.add_argument("--out", default=os.path.join(HERE, "dist", "TriggerComplete.oxt"))
    ap.add_argument("--install", action="store_true", help="install with unopkg (close LibreOffice first)")
    args = ap.parse_args()
    out = build(os.path.abspath(os.path.expanduser(args.config_dir)), os.path.abspath(args.out))
    print("built", out)
    if args.install:
        subprocess.run(["unopkg", "add", "--force", out], check=True)
        print("installed; start LibreOffice Writer")


if __name__ == "__main__":
    sys.exit(main())
