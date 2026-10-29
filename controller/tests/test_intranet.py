import pytest

from ludius import intranet

PAGE = "---\ntitle: T\nsection: S\nowner: O\nupdated: 2026-01-02\ntags: a, b c\n---\nOne\nline.\n\nTwo.\n"


def test_shipped_pages_and_the_rules():
    assert any(p.id == "forklift-check" and p.popular for p in intranet.load())
    p = intranet.parse("x", PAGE)
    assert (p.tags, p.body, p.popular) == (("a", "b c"), ("One line.", "Two."), False)
    assert intranet.parse("x", PAGE + "See http://intranet.corp.ludius.test/?a=x\n")
    for id, text in [("X", PAGE), ("x", PAGE.replace("owner: O\n", "")), ("x", PAGE.replace("2026-01-02", "soon")),
                     ("x", PAGE + "http://example.com/\n"), ("x", PAGE + "https://corp.ludius.test:443@example.com/\n"),
                     ("x", PAGE + "see //example.com/x\n"),
                     ("x", PAGE.replace("tags", "color")), ("x", PAGE.split("---\n")[1])]:  # fmt: skip
        with pytest.raises(ValueError):
            intranet.parse(id, text)
