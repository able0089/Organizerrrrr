"""
keep_alive.py
-------------
Runs a minimal Flask web server in a background thread.
Render's free tier spins down instances that receive no HTTP traffic,
so an external uptime monitor (e.g. UptimeRobot) can ping the /ping
endpoint to keep the bot alive.
"""

import threading
from flask import Flask

app = Flask(__name__)


@app.route("/")
def home():
    return "Bot is running."


@app.route("/ping")
def ping():
    return "pong"


def run():
    # Run on 0.0.0.0 so Render's port binding is satisfied.
    # Render injects the PORT env var; fall back to 8080 locally.
    import os
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)


def keep_alive():
    """Start the Flask server in a daemon thread so it doesn't block the bot."""
    thread = threading.Thread(target=run, daemon=True)
    thread.start()
