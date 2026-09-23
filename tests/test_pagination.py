"""Out-of-range ?page= handling on the paginated list views."""

from urllib.parse import parse_qs, urlsplit

import pytest

from app.extensions import db
from app.models.user import UserAccount
from app.routes.users import _PAGE_SIZE as USERS_PAGE_SIZE

LIST_URLS = ["/users/", "/admin/audit-log/", "/events/"]


def _query(resp) -> dict[str, list[str]]:
    # keep_blank_values: an empty filter (e.g. types=) is meaningful and must survive.
    return parse_qs(urlsplit(resp.headers["Location"]).query, keep_blank_values=True)


@pytest.mark.parametrize("url", LIST_URLS)
@pytest.mark.parametrize("page", ["0", "-1", "abc"])
def test_page_below_one_renders_first_page(admin_client, url, page):
    assert admin_client.get(f"{url}?page={page}").status_code == 200


@pytest.mark.parametrize("url", LIST_URLS)
def test_huge_page_redirects_instead_of_overflowing(admin_client, url):
    resp = admin_client.get(f"{url}?page=9999999999999999999999")
    assert resp.status_code == 302
    assert "page" not in _query(resp)


@pytest.mark.parametrize(
    "url, filters",
    [
        ("/users/", {"q": ["admin"], "sort": ["email"], "dir": ["desc"]}),
        ("/admin/audit-log/", {"entity_type": ["Event"], "q": ["x"]}),
        ("/events/", {"statuses": ["DRAFT,PUBLISHED"], "types": [""], "sort": ["name"]}),
    ],
)
def test_page_beyond_last_redirect_keeps_filters(admin_client, url, filters):
    qs = "&".join(f"{k}={v[0]}" for k, v in filters.items())
    resp = admin_client.get(f"{url}?{qs}&page=5")
    assert resp.status_code == 302
    assert urlsplit(resp.headers["Location"]).path == url
    assert _query(resp) == filters


def _fill_two_user_pages(app) -> None:
    # One full page plus admin_client's own user ("Test Admin", sorts first)
    # spills the last seeded user, "User NNN", onto a second page.
    with app.app_context():
        db.session.add_all(
            UserAccount(email=f"u{i:03}@test.com", name=f"User {i:03}", password_hash="x", is_active=True)
            for i in range(USERS_PAGE_SIZE)
        )
        db.session.commit()


def test_users_page_below_one_shows_first_slice(app, admin_client):
    _fill_two_user_pages(app)
    html = admin_client.get("/users/?page=0").get_data(as_text=True)
    assert "User 000" in html
    assert f"User {USERS_PAGE_SIZE - 1:03}" not in html


def test_users_page_beyond_last_lands_on_last_page(app, admin_client):
    _fill_two_user_pages(app)
    resp = admin_client.get("/users/?page=99")
    assert resp.status_code == 302
    assert _query(resp) == {"page": ["2"]}
    assert admin_client.get(resp.headers["Location"]).status_code == 200


def test_redirect_keeps_url_for_reserved_and_repeated_keys_as_plain_query(admin_client):
    resp = admin_client.get("/users/?page=5&_anchor=x&_method=GET&_external=1&a=1&a=2")
    assert resp.status_code == 302
    assert resp.headers["Location"].startswith("/users/?")
    assert _query(resp) == {"_anchor": ["x"], "_method": ["GET"], "_external": ["1"], "a": ["1", "2"]}
