"""
Vercel Python serverless entry point for the Sesly bot.

Vercel's @vercel/python builder treats a module-level `app` that's a WSGI
callable as the handler. We import the Flask app defined in app.py and
re-export it here.
"""
import sys
from pathlib import Path

# Make sibling modules (app, ai, db, router, voice) importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import app  # noqa: E402,F401
