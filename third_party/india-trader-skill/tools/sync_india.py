"""Copy india/india.py into every bharat-* skill folder (each installs on its own)."""

import os
import shutil

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "india", "india.py")

for d in sorted(os.listdir(ROOT)):
    if d.startswith("bharat-") and os.path.isdir(os.path.join(ROOT, d)):
        shutil.copyfile(SRC, os.path.join(ROOT, d, "india.py"))
        print("synced", d)
