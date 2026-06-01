"""E2E tests: smoke-test all main pages (assert they load without errors)."""

import pytest

# Routes to visit with expected text on the page (heading or distinctive element).
MAIN_ROUTES = [
    ("/dashboard", ["Přehled"]),
    ("/events/", ["Akce"]),
    ("/master-events/", ["Nadřazené akce"]),
    ("/users/", ["Uživatel", "Správa uživatelů"]),
    ("/equipment/", ["Vybavení"]),
    ("/admin/audit-log", ["Audit", "Historie"]),
    ("/admin/settings", ["Nastavení"]),
    ("/users/profile", ["Profil"]),
    ("/changelog", ["Změny", "Changelog"]),
]


@pytest.mark.parametrize(
    "path,expected_texts", MAIN_ROUTES, ids=[r[0].strip("/").replace("/", "-") or "dashboard" for r in MAIN_ROUTES]
)
def test_page_loads(logged_in_page, base_url, path, expected_texts):
    """Each main page should load with HTTP 200 and expected content."""
    page = logged_in_page
    resp = page.goto(f"{base_url}{path}")

    # Assert no server error
    assert resp is not None
    assert resp.status < 400, f"{path} returned {resp.status}"

    # At least one of the expected texts should appear on the page
    body_text = page.locator("body").inner_text()
    found = any(t.lower() in body_text.lower() for t in expected_texts)
    assert found, f"{path}: none of {expected_texts} found in page body " f"(first 300 chars: {body_text[:300]})"
