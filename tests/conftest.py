import os
import sys

# Run from the backend root: `pytest tests/` — make `app` importable and keep logs/ local.
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)
