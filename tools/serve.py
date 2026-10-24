#!/usr/bin/env python3
"""
Local web server for Bluewater (replacement for `python -m http.server`).

- Serves pre-compressed *.gz copies (from build_snapshots.py) with gzip encoding,
  cutting snapshot transfer size about 4-5x.
- Sends no-cache for index.html and manifest.json (always revalidated) and long-lived
  caching for versioned snapshot requests (?v=...).

Usage (from the app folder):  python tools/serve.py [port]     default port 8000
"""
import http.server
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import os
import pathlib
import re
import threading
import urllib.parse

from bathymetry import Bathymetry, default_directory

ROOT = pathlib.Path(__file__).resolve().parent.parent


class Handler(http.server.SimpleHTTPRequestHandler):
    extensions_map = {**http.server.SimpleHTTPRequestHandler.extensions_map,
                      ".js": "text/javascript", ".json": "application/json"}

    def __init__(self, *args, **kwargs):
        self._cache_control = None
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def _bytes(self, status, payload, content_type, cache="no-store"):
        self._cache_control = cache
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            # Map navigation routinely cancels tile requests; retain the completed cache entry.
            self.close_connection = True

    def _json(self, status, payload):
        self._bytes(status, json.dumps(payload, allow_nan=False).encode(), "application/json; charset=utf-8")

    def do_GET(self):
        self._cache_control = None
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path == "/api/bathymetry/status":
            self._json(200, self.server.bathymetry.status())
            return
        match = re.fullmatch(r"/api/bathymetry/hillshade/(\d+)/(\d+)/(\d+)\.png", parsed.path)
        if match:
            engine = self.server.bathymetry
            state = engine.status()
            if not state["ready"]:
                self._json(503, {"error": state["message"]})
                return
            requested = urllib.parse.parse_qs(parsed.query).get("v", [None])[0]
            if requested and requested != state["version"]:
                self._json(409, {"error": "The bathymetry version changed. Refresh the page."})
                return
            if not self.server.raster_slots.acquire(timeout=10):
                self._json(503, {"error": "The local raster service is busy. Try again shortly."})
                return
            try:
                payload = engine.tile(*(int(part) for part in match.groups()), version=requested)
                cache = "public, max-age=31536000, immutable" if requested else "no-cache"
                self._bytes(200, payload, "image/png", cache)
            except ValueError as exc:
                self._json(400, {"error": str(exc)})
            except Exception as exc:
                self.log_error("Local hillshade failed: %s", exc)
                self._json(503, {"error": "The local hillshade raster could not be read."})
            finally:
                self.server.raster_slots.release()
            return
        if parsed.path.startswith("/api/"):
            self._json(404, {"error": "Unknown local API route."})
            return
        super().do_GET()

    def do_HEAD(self):
        if urllib.parse.urlsplit(self.path).path.startswith("/api/"):
            self.do_GET()
        else:
            super().do_HEAD()

    def do_POST(self):
        self._cache_control = None
        if urllib.parse.urlsplit(self.path).path != "/api/bathymetry/sample":
            self._json(404, {"error": "Unknown local API route."})
            self.close_connection = True
            return
        try:
            size = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            size = 0
        if not 0 < size <= 1024*1024:
            self._json(413, {"error": "Send a JSON request smaller than 1 MB."})
            self.close_connection = True
            return
        try:
            request = json.loads(self.rfile.read(size))
            if not isinstance(request, dict):
                raise ValueError("Send a JSON object containing points and an optional method.")
        except (ValueError, UnicodeDecodeError) as exc:
            self._json(400, {"error": str(exc)})
            return
        engine = self.server.bathymetry
        state = engine.status()
        if not state["ready"]:
            self._json(503, {"error": state["message"]})
            return
        if request.get("version") and request["version"] != state["version"]:
            self._json(409, {"error": "The bathymetry version changed. Run Show Elevation again."})
            return
        if not self.server.raster_slots.acquire(timeout=10):
            self._json(503, {"error": "The local raster service is busy. Try again shortly."})
            return
        try:
            self._json(200, engine.sample(request.get("points"), request.get("method", "nearest")))
        except ValueError as exc:
            self._json(400, {"error": str(exc)})
        except Exception as exc:
            self.log_error("Local bathymetry sampling failed: %s", exc)
            self._json(503, {"error": "The local bathymetry raster could not be read."})
        finally:
            self.server.raster_slots.release()

    def send_head(self):
        parsed = urllib.parse.urlsplit(self.path)
        local = pathlib.Path(self.translate_path(parsed.path))
        gz = local.with_name(local.name + ".gz")
        accepts_gzip = "gzip" in self.headers.get("Accept-Encoding", "")
        if accepts_gzip and local.suffix in {".json", ".js"} and gz.is_file():
            try:
                stat = gz.stat()
                if local.is_file() and local.stat().st_mtime > stat.st_mtime:
                    raise FileNotFoundError  # stale .gz; serve the plain file
                f = open(gz, "rb")
            except OSError:
                return super().send_head()
            self.send_response(200)
            self.send_header("Content-Type", self.guess_type(str(local)))
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Content-Length", str(stat.st_size))
            self.send_header("Last-Modified", self.date_time_string(stat.st_mtime))
            self.send_header("Vary", "Accept-Encoding")
            self.end_headers()
            return f
        return super().send_head()

    def end_headers(self):
        parsed = urllib.parse.urlsplit(self.path)
        if self._cache_control:
            self.send_header("Cache-Control", self._cache_control)
        elif "v=" in parsed.query:
            self.send_header("Cache-Control", "public, max-age=31536000, immutable")
        else:
            self.send_header("Cache-Control", "no-cache")
        super().end_headers()


class LocalServer(http.server.ThreadingHTTPServer):
    """Reuse workers and their raster handles, while leaving capacity for ordinary files."""
    def __init__(self, address, handler):
        self.workers = ThreadPoolExecutor(max_workers=12, thread_name_prefix="bluewater")
        super().__init__(address, handler)

    def process_request(self, request, client_address):
        self.workers.submit(self.process_request_thread, request, client_address)

    def server_close(self):
        super().server_close()
        self.workers.shutdown(wait=True)


def create_server(host="127.0.0.1", port=8000, bathymetry_directory=None):
    server = LocalServer((host, port), Handler)
    server.bathymetry = Bathymetry(bathymetry_directory)
    server.raster_slots = threading.BoundedSemaphore(4)
    return server


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("port", nargs="?", type=int, default=8000)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--bathymetry-dir", type=pathlib.Path, default=default_directory())
    args = parser.parse_args()
    os.chdir(ROOT)
    with create_server(args.host, args.port, args.bathymetry_dir) as httpd:
        state = httpd.bathymetry.status()
        print(f"Serving {ROOT} at http://{args.host}:{args.port}/  (Ctrl+C to stop)", flush=True)
        print(f"Local bathymetry: {state.get('version') if state['ready'] else state['message']}", flush=True)
        httpd.serve_forever()
