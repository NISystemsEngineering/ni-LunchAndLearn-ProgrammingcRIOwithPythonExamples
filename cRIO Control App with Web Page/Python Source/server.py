#!/usr/bin/env python3
"""
CompactRIO Variable Dashboard Server
=====================================
A lightweight HTTP server for NI Linux RT that serves a live dashboard
showing control application variables.

Architecture:
  - Your control app writes variable values to /tmp/crio_variables.json
  - This server reads that file and serves it at GET /data
  - The dashboard HTML page polls /data every second

Usage:
  python3 server.py              # starts on port 8080
  python3 server.py --port 9000  # custom port

Then open http://<crio-ip>:8080 in a browser.
"""

import json
import os
import time
import argparse
from http.server import HTTPServer, SimpleHTTPRequestHandler

# --- Configuration -----------------------------------------------------------

VARIABLES_FILE = "/tmp/crio_variables.json"
DASHBOARD_DIR = os.path.dirname(os.path.abspath(__file__))

# --- Request Handler ---------------------------------------------------------

class DashboardHandler(SimpleHTTPRequestHandler):
    """Serves the dashboard HTML and a /data JSON endpoint."""

    def __init__(self, *args, **kwargs):
        # Serve files from the dashboard directory
        super().__init__(*args, directory=DASHBOARD_DIR, **kwargs)

    def do_GET(self):
        if self.path == "/data":
            self._serve_data()
        elif self.path == "/" or self.path == "/index.html":
            self.path = "/index.html"
            super().do_GET()
        else:
            super().do_GET()

    def _serve_data(self):
        """Read the shared JSON file and return its contents."""
        try:
            with open(VARIABLES_FILE, "r") as f:
                data = json.load(f)

            # Add server timestamp
            data["_server_time"] = time.time()

            payload = json.dumps(data).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        except FileNotFoundError:
            self._send_error(
                503,
                "Waiting for control application — "
                f"{VARIABLES_FILE} not found yet."
            )
        except json.JSONDecodeError:
            self._send_error(500, "Variables file contains invalid JSON.")
        except Exception as e:
            self._send_error(500, str(e))

    def _send_error(self, code, message):
        payload = json.dumps({"error": message}).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    # Suppress default logging clutter
    def log_message(self, format, *args):
        if "/data" not in args[0]:  # only log page requests, not polls
            super().log_message(format, *args)

# --- Main --------------------------------------------------------------------

def webpage_main():
    parser = argparse.ArgumentParser(description="CompactRIO Dashboard Server")
    parser.add_argument("--port", type=int, default=8181, help="HTTP port (default 8080)")
    parser.add_argument("--demo", action="store_true",
                        help="Continuously write simulated data for testing")
    args = parser.parse_args()

    print(f"Dashboard running at http://192.168.68.55:{args.port}")
    print(f"Reading variables from {VARIABLES_FILE}")
    server = HTTPServer(("192.168.68.55", args.port), DashboardHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        server.server_close()

if __name__ == "__main__":
    webpage_main()