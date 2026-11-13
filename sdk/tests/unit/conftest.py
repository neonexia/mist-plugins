"""A zero-delay local S3 server shared by the unit tests (test_s3.py, test_recipes.py)."""
import hashlib
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest


class QuietServer(ThreadingHTTPServer):
    def handle_error(self, request, client_address):  # a keep-alive socket closed at shutdown is not news
        pass


def _error(code):
    return f"<Error><Code>{code}</Code><Message>no</Message></Error>"


class S3Server:
    """Records (method, path, access key, headers). Keeps PUT bodies and answers GET/HEAD from them, with ETags;
    honours If-None-Match: * and If-Match on PUT (412 otherwise). Refuses the keys in `deny` with 403.
    A GET of a key never written answers `default` (a body) or 404 when `default` is None; any '/missing' key is 404.
    `put_script` is a list of statuses used up by the next PUTs (a scripted 412 for a lost race): 412 stores nothing."""

    def __init__(self, deny=(), default=b"hello", put_script=()):
        self.deny = set(deny)
        self.default = default
        self.put_script = list(put_script)
        self.objects = {}  # path -> body
        self.seen = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"  # answers Expect: 100-continue

            def log_message(self, *a):
                pass

            def handle_one(self):
                n = int(self.headers.get("Content-Length") or 0)
                put_body = self.rfile.read(n) if n else b""
                ak = self.headers["Authorization"].split("Credential=")[1].split("/")[0]
                outer.seen.append((self.command, self.path, ak, dict(self.headers)))
                etag = '"x"'
                body = b""
                if ak in outer.deny:
                    status, body = 403, _error("AccessDenied").encode()
                elif self.command == "PUT":
                    status = self.put_status()
                    if status == 200:
                        outer.objects[self.path] = put_body
                    elif status == 412:
                        body = _error("PreconditionFailed").encode()
                    etag = outer.etag(self.path)
                elif self.path.endswith("/missing") or (self.path not in outer.objects and outer.default is None):
                    status, body = 404, _error("NoSuchKey").encode()
                else:
                    body = outer.objects.get(self.path, outer.default)
                    status = 200
                    etag = outer.etag(self.path)
                self.send_response(status)
                self.send_header("ETag", etag)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)  # a PUT answers an empty body: botocore reads a 200 PutObject body as an error

            def put_status(self):
                if outer.put_script:
                    return outer.put_script.pop(0)
                h = {k.lower(): v for k, v in self.headers.items()}
                exists = self.path in outer.objects
                if h.get("if-none-match") == "*" and exists:
                    return 412
                if "if-match" in h and (not exists or h["if-match"] != outer.etag(self.path)):
                    return 412
                return 200

            do_GET = do_PUT = do_HEAD = handle_one

        self.server = QuietServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()

    def etag(self, path):
        return '"%s"' % hashlib.md5(self.objects[path]).hexdigest() if path in self.objects else '"x"'

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server.server_port}"

    @property
    def keys(self):
        return [s[2] for s in self.seen]

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def s3server(request):
    servers = []

    def make(deny=(), **kw):
        s = S3Server(deny, **kw)
        servers.append(s)
        return s

    yield make
    for s in servers:
        s.close()
