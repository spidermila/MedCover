"""E2E tests: label accessibility — every label must be bound to a form control.

Regression coverage for:
- PR #218: Fix label accessibility: add for/id bindings across templates
"""
import pytest

# Pages with forms that should have proper label bindings.
LABEL_PAGES = [
    "/events/create",
    "/users/profile",
    "/admin/settings",
]


@pytest.mark.parametrize("path", LABEL_PAGES,
                         ids=[p.strip("/").replace("/", "-") for p in LABEL_PAGES])
def test_labels_have_for_attribute(logged_in_page, base_url, path):
    """Every <label> with a 'for' attribute must reference an existing element."""
    page = logged_in_page
    page.goto(f"{base_url}{path}")

    labels = page.locator("label[for]")
    count = labels.count()
    if count == 0:
        return

    for i in range(count):
        label = labels.nth(i)
        for_value = label.get_attribute("for")
        assert for_value, f"Label #{i} on {path} has empty 'for' attribute"

        target = page.locator(f"#{for_value}")
        assert target.count() > 0, (
            f"Label '{label.inner_text()[:40]}' on {path} has for='{for_value}' "
            f"but no element with id='{for_value}' exists"
        )
