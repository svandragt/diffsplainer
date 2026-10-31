from fearful_owl import github


def _pages(monkeypatch, sizes: list[int]) -> list[str]:
    """Serve one page per entry in `sizes`; returns the requested paths."""
    requested: list[str] = []

    def fake(path, **_):
        requested.append(path)
        page = int(path.rsplit("page=", 1)[1])
        return [{"filename": f"f{page}-{i}"} for i in range(sizes[page - 1])]

    monkeypatch.setattr(github, "gh", fake)
    return requested


def test_get_pull_files_reads_past_ten_pages_until_a_short_page(monkeypatch):
    requested = _pages(monkeypatch, [100] * 25 + [40])
    assert len(github.get_pull_files("o", "r", 1)) == 2540
    assert len(requested) == 26


def test_get_pull_files_stops_at_githubs_3000_file_limit(monkeypatch):
    requested = _pages(monkeypatch, [100] * 40)
    assert len(github.get_pull_files("o", "r", 1)) == 3000
    assert len(requested) == 30
