import os
import sys
import tempfile
from pathlib import Path

# Configure an isolated, offline environment before the app modules are imported.
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="dd-test-")
os.environ["FORCE_MOCK"] = "1"
os.environ["ANTHROPIC_FALLBACKS"] = "0"
os.environ.pop("ANTHROPIC_API_KEY", None)
os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
