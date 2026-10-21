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
import os
import pathlib
import sys
import urllib.parse

ROOT = pathlib.Path(__file__).resolve().parent.parent


class Handler(http.server.SimpleHTTPRequestHandler):
    extensions_map = {**http.server.SimpleHTTPRequestHandler.extensions_map,
                      ".js": "text/javascript", ".json": "application/json"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

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
        if "v=" in parsed.query:
            self.send_header("Cache-Control", "public, max-age=31536000, immutable")
        else:
            self.send_header("Cache-Control", "no-cache")
        super().end_headers()


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    os.chdir(ROOT)
    with http.server.ThreadingHTTPServer(("", port), Handler) as httpd:
        print(f"Serving {ROOT} at http://localhost:{port}/  (Ctrl+C to stop)")
        httpd.serve_forever()
