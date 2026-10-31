import socket
import threading
import time
from http.server import ThreadingHTTPServer

import pytest

from diffsplainer import server


@pytest.fixture
def serve():
    """The app's HTTP server on a free port; yields the server, whose port is `server_port`."""
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:  # shutdown() hangs if serve_forever() has not started yet
        try:
            socket.create_connection(("127.0.0.1", httpd.server_port), timeout=0.1).close()
            break
        except OSError:
            time.sleep(0.01)
    yield httpd
    httpd.shutdown()
    httpd.server_close()
