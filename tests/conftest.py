import threading
from http.server import ThreadingHTTPServer

import pytest

from diffsplainer import server


@pytest.fixture
def serve():
    """The app's HTTP server on a free port; yields the server, whose port is `server_port`."""
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield httpd
    httpd.shutdown()
    httpd.server_close()
