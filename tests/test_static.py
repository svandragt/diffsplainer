import re
import threading
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from diffsplainer import server

VENDOR = server.STATIC / "vendor"


def test_prism_and_its_licence_are_vendored():
    assert (VENDOR / "prism.js").is_file()
    assert "MIT" in (VENDOR / "prism-LICENSE.txt").read_text()


def test_index_loads_prism_from_the_app_before_app_js_and_nothing_from_outside():
    html = (server.STATIC / "index.html").read_text()
    srcs = re.findall(r'<script[^>]*\ssrc="([^"]+)"', html)
    assert srcs == ["/vendor/prism.js", "/app.js"]
    assert not re.search(r"""(?:src|href)=["']https?:""", html)


@pytest.fixture
def get():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    def fetch(path):
        with urllib.request.urlopen(f"http://127.0.0.1:{httpd.server_port}{path}") as res:
            return res.status, res.headers["Content-Type"], res.read()

    yield fetch
    httpd.shutdown()
    httpd.server_close()


def test_prism_is_served_as_javascript(get):
    status, ctype, body = get("/vendor/prism.js")
    assert status == 200
    assert "javascript" in ctype  # a missing file would fall back to index.html as text/html
    assert b"Prism.languages" in body
