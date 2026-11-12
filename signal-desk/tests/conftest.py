import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# tests never touch the real data/state/ (the app reads this when the store is first created)
os.environ["SIGNAL_DESK_STATE_DIR"] = tempfile.mkdtemp(prefix="signal-desk-test-state-")
