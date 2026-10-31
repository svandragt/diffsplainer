import re
import urllib.request

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
def get(serve):
    def fetch(path):
        with urllib.request.urlopen(f"http://127.0.0.1:{serve.server_port}{path}") as res:
            return res.status, res.headers["Content-Type"], res.read()

    return fetch


def test_prism_is_served_as_javascript(get):
    status, ctype, body = get("/vendor/prism.js")
    assert status == 200
    assert "javascript" in ctype  # a missing file would fall back to index.html as text/html
    assert b"Prism.languages" in body


def test_every_language_app_js_maps_an_extension_to_has_a_grammar_in_prism():
    js = (server.STATIC / "app.js").read_text()
    ids = set(re.findall(r":\s*'(\w+)'", re.search(r"const LANGS = \{(.*?)\};", js, re.DOTALL).group(1)))
    assert {"python", "javascript"} <= ids  # the map was found
    prism = (VENDOR / "prism.js").read_text()
    assert [i for i in sorted(ids) if not re.search(rf"languages\.{i}=", prism)] == []
