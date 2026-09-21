"""Headless / preview entry point for RainDance.

Always serves the app as a plain browser app (never a native window), by
forcing web mode via the environment *before* importing app. This is what
.claude/launch.json runs, so it doesn't depend on CLI args reaching the process.

    .venv/bin/python serve.py     # → http://localhost:8213
"""
import os

os.environ["RAINDANCE_WEB"] = "1"

import app  # noqa: E402 - env must be set before app is imported

if __name__ == "__main__":
    app.run()
