import re
import urllib.request

import pytest

from diffsplainer import server

VENDOR = server.STATIC / "vendor"
# a src/href whose value has a scheme (other than javascript:/data:) or is protocol-relative
EXTERNAL_URL = r"""\b(?:src|href)\s*=\s*["'](?:(?!javascript:|data:)[a-z][a-z0-9+.-]*:|//)"""


def test_prism_and_its_licence_are_vendored():
    assert (VENDOR / "prism.js").is_file()
    assert "MIT" in (VENDOR / "prism-LICENSE.txt").read_text()


def test_index_loads_prism_from_the_app_before_app_js_and_nothing_from_outside():
    html = (server.STATIC / "index.html").read_text()
    srcs = re.findall(r'<script[^>]*\ssrc="([^"]+)"', html)
    assert srcs == ["/vendor/prism.js", "/app.js"]
    loaded = re.sub(r"<a\b[^>]*>", "", html)  # links the user clicks load nothing
    assert not re.search(EXTERNAL_URL, loaded, re.IGNORECASE)


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


def name_lang(rules, path):
    """The language of the first rule matching the base name, as langOf does; '' if none."""
    base = path.split("/")[-1]
    return next((lang for rx, lang in rules if rx.search(base)), "")


def test_filename_rules_map_dockerfile_makefile_mk_and_env_variants_and_nothing_else():
    js = (server.STATIC / "app.js").read_text()
    body = re.search(r"const NAME_LANGS = \[(.*?)\];", js, re.DOTALL).group(1)
    rules = []
    for src, flags, lang in re.findall(r"\[/(.+?)/([a-z]*),\s*'(\w+)'\]", body):
        assert set(flags) <= {"i"}, f"flags {flags!r} on /{src}/ are not handled by this test"
        try:
            rules.append((re.compile(src, re.IGNORECASE if "i" in flags else 0), lang))
        except re.error as e:
            pytest.fail(f"/{src}/ is not valid in Python re, so this test cannot check it: {e}")
    assert len(rules) == body.count("[/")  # every entry was parsed
    expected = {
        "docker": ["Dockerfile", "Dockerfile.dev", "a/b/Dockerfile"],
        "makefile": ["Makefile", "GNUmakefile", "x.mk", "src/build.mk"],
        "ini": [".env.local", ".env.production"],
        "": ["Dockerfilex", "MyMakefile.txt", "env.local", "notes.txt"],
    }
    actual = {lang: [n for n in names if name_lang(rules, n) == lang] for lang, names in expected.items()}
    assert actual == expected
    prism = (VENDOR / "prism.js").read_text()
    assert [i for i in ("docker", "makefile", "ini") if not re.search(rf"languages\.{i}=", prism)] == []
