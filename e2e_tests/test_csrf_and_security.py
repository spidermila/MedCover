"""E2E tests: CSRF tokens and form integrity across all major pages.

Regression coverage for:
- PR #180: template lint for unclosed <form> tags and missing csrf_token
- PR #179: missing > on digest delete form causes CSRF 400
- PR #170: missing closing > on feedback delete form caused CSRF error
- Issue #335: missing > on equipment item forms caused CSRF error
"""
import pytest


def _discover_app_pages(page, base_url):
    """Crawl the navbar and collect all internal links to check."""
    page.goto(f"{base_url}/dashboard")
    links = page.locator("a[href]").all()
    paths = set()
    for link in links:
        href = link.get_attribute("href") or ""
        # Only internal links
        if href.startswith("/"):
            # Skip logout, static, and fragment-only links
            if any(skip in href for skip in ["/auth/logout", "/static/", "#"]):
                continue
            paths.add(href)
        elif href.startswith(base_url):
            path = href[len(base_url):]
            if path and "/auth/logout" not in path and "/static/" not in path:
                paths.add(path)
    return sorted(paths)


# Pages known to contain POST forms.  The dynamic discovery test below covers
# these AND any others reachable from the navbar.
FORM_PAGES = [
    "/events/create",
    "/users/profile",
    "/admin/settings",
    "/admin/digest/",
    "/changelog",
    "/equipment/items",
    "/qualifications/",
    "/users/invites",
]


def test_all_post_forms_have_csrf_token_dynamic(logged_in_page, base_url):
    """Discover all pages from navbar links and verify every POST form has csrf_token.

    This catches malformed <form> tags (missing closing >) where the csrf_token
    input ends up outside the form element.
    """
    page = logged_in_page
    discovered = _discover_app_pages(page, base_url)
    # Merge with known form pages to ensure coverage
    all_pages = sorted(set(discovered) | set(FORM_PAGES))

    violations = []
    for path in all_pages:
        try:
            resp = page.goto(f"{base_url}{path}", wait_until="domcontentloaded", timeout=10000)
        except Exception:
            continue
        if resp is None or resp.status >= 400:
            continue

        forms = page.locator('form[method="POST"], form[method="post"]')
        count = forms.count()
        for i in range(count):
            form = forms.nth(i)
            csrf = form.locator('input[name="csrf_token"]')
            if csrf.count() == 0:
                action = form.get_attribute("action") or "(no action)"
                violations.append(f"{path} → form action={action}")

    assert not violations, (
        f"Found {len(violations)} POST form(s) missing csrf_token inside the <form> element:\n"
        + "\n".join(f"  • {v}" for v in violations)
    )


@pytest.mark.parametrize(
    "path",
    FORM_PAGES,
    ids=[p.strip("/").replace("/", "-") for p in FORM_PAGES],
)
def test_known_form_pages_have_csrf_token(logged_in_page, base_url, path):
    """Targeted check for known form pages (parametrized for clear failure reporting)."""
    page = logged_in_page
    resp = page.goto(f"{base_url}{path}")
    if resp is None or resp.status >= 400:
        pytest.skip(f"{path} returned {resp.status if resp else 'None'}")

    forms = page.locator('form[method="POST"], form[method="post"]')
    count = forms.count()
    if count == 0:
        return  # Page has no POST forms — nothing to check

    for i in range(count):
        form = forms.nth(i)
        csrf = form.locator('input[name="csrf_token"]')
        assert csrf.count() > 0, (
            f"Form #{i} on {path} has no csrf_token input. Action: {form.get_attribute('action')}"
        )


def test_no_inline_event_handlers(logged_in_page, base_url):
    """No element should have onclick, onchange, oninput etc. (CSP compliance).

    Regression coverage for PR #160, #159.
    """
    page = logged_in_page
    pages_to_check = [
        "/events/create",
        "/users/profile",
        "/dashboard",
    ]

    for path in pages_to_check:
        page.goto(f"{base_url}{path}")
        # Check for any inline event handler attributes
        handlers = [
            "onclick",
            "onchange",
            "oninput",
            "onsubmit",
            "onblur",
            "onfocus",
            "onkeydown",
            "onkeyup",
        ]
        for handler in handlers:
            count = page.locator(f"[{handler}]").count()
            assert count == 0, (
                f"Found {count} element(s) with {handler}= on {path}. "
                f"Inline handlers are forbidden by CSP."
            )
