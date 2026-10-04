#!/usr/bin/env python3
"""Serves the phase review page and zip downloads on port 8080."""

from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def end_headers(self) -> None:
        if self.path.split("?", 1)[0].endswith(".zip"):
            name = Path(self.path.split("?", 1)[0]).name
            self.send_header("Content-Disposition", f'attachment; filename="{name}"')
        super().end_headers()


def main() -> None:
    server = ThreadingHTTPServer(("0.0.0.0", 8080), Handler)
    server.serve_forever()


if __name__ == "__main__":
    main()
