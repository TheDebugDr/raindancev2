"""Entry point alias — same as `python app.py --web` for the combined app."""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
os.environ.setdefault("RAINDANCE_WEB", "1")

import app  # noqa: E402

if __name__ == "__main__":
    app.run()
