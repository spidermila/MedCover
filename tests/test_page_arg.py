"""Tests for the page_arg query-string helper."""

import pytest

from app.utils import MAX_PAGE, page_arg


@pytest.mark.parametrize(
    "query, expected",
    [
        ("", 1),
        ("page=3", 3),
        ("page=0", 1),
        ("page=-3", 1),
        ("page=", 1),
        ("page=abc", 1),
        ("page=1.5", 1),
        (f"page={MAX_PAGE}", MAX_PAGE),
        ("page=9999999999999999999999", MAX_PAGE),
    ],
)
def test_page_arg_clamps_and_defaults(app, query, expected):
    with app.test_request_context(f"/?{query}"):
        assert page_arg() == expected
