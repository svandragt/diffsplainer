from fearful_owl.diff import LONG_HUNK, parse_hunks, split_hunk


def test_trailing_newline_adds_no_context_line():
    patch = "@@ -1,2 +1,2 @@\n a\n-b\n+c\n"
    assert [l["t"] for l in parse_hunks(patch)[0]["lines"]] == ["ctx", "del", "add"]


def test_unicode_line_separator_stays_inside_its_line():
    patch = "@@ -0,0 +1,1 @@\n+a b\n"
    assert [l["s"] for l in parse_hunks(patch)[0]["lines"]] == ["a b"]


def test_parse_hunks_numbers_each_line_kind():
    patch = "@@ -10,3 +20,4 @@ def f():\n keep\n-old\n+new1\n+new2\n tail\n\\ No newline at end of file"
    (hunk,) = parse_hunks(patch)
    assert hunk["scope"] == "def f():"
    assert [(l["t"], l["o"], l["n"], l["s"]) for l in hunk["lines"]] == [
        ("ctx", 10, 20, "keep"),
        ("del", 11, None, "old"),
        ("add", None, 21, "new1"),
        ("add", None, 22, "new2"),
        ("ctx", 12, 23, "tail"),
    ]


def test_parse_hunks_restarts_numbering_per_hunk():
    patch = "@@ -1 +1 @@\n-a\n+b\n@@ -50,2 +60,2 @@\n x\n+y"
    first, second = parse_hunks(patch)
    assert [(l["o"], l["n"]) for l in first["lines"]] == [(1, None), (None, 1)]
    assert [(l["o"], l["n"]) for l in second["lines"]] == [(50, 60), (None, 61)]


def _lines(kind: str, texts: list[str]) -> list[dict]:
    return [{"t": kind, "o": None, "n": i, "s": s} for i, s in enumerate(texts, 1)]


def test_split_hunk_keeps_short_hunk_whole():
    lines = _lines("add", ["x"] * LONG_HUNK)
    assert split_hunk(lines) == [lines]


def test_split_hunk_splits_at_long_context_run_keeping_three_lines_either_side():
    lines = _lines("add", ["a"] * 15) + _lines("ctx", ["c"] * 10) + _lines("add", ["b"] * 15)
    first, second = split_hunk(lines)
    assert [l["t"] for l in first] == ["add"] * 15 + ["ctx"] * 3
    assert [l["t"] for l in second] == ["ctx"] * 3 + ["add"] * 15


def test_split_hunk_splits_new_code_at_top_level_block_after_blank_line():
    first_fn = ["def a():"] + ["    pass"] * 13 + [""]
    second_fn = ["def b():"] + ["    pass"] * 15
    parts = split_hunk(_lines("add", first_fn + second_fn))
    assert [len(p) for p in parts] == [len(first_fn), len(second_fn)]
    assert parts[1][0]["s"] == "def b():"
