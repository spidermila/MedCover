"""E2E tests: CSRF tokens and form integrity across all major pages.

Regression coverage for:
- PR #180: template lint for unclosed <form> tags and missing csrf_token
- PR #179: missing > on digest delete form causes CSRF 400
- PR #170: missing closing > on feedback delete form caused CSRF error
"""
import pytest

# Pages that contain POST forms requiring CSRF tokens.
FORM_PAGES = [
    "/events/create",
    "/users/profile",
    "/admin/settings",
    "/admin/digest/",
    "/changelog",
]


@pytest.mark.parametrize("path", FORM_PAGES,
                         ids=[p.strip("/").replace("/", "-") for p in FORM_PAGES])
def test_all_post_forms_have_csrf_token(logged_in_page, base_url, path):
    """Every <form method='POST'> must contain a csrf_token hidden input."""
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
            f"Form #{i} on {path} has no csrf_token input. "
            f"Action: {form.get_attribute('action')}"
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
            "onclick", "onchange", "oninput", "onsubmit",
            "onblur", "onfocus", "onkeydown", "onkeyup",
        ]
        for handler in handlers:
            count = page.locator(f"[{handler}]").count()
            assert count == 0, (
                f"Found {count} element(s) with {handler}= on {path}. "
                f"Inline handlers are forbidden by CSP."
            )
