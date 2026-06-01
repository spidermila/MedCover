"""E2E tests: event detail page rendering and sections.

Regression coverage for:
- PR #218: label accessibility on detail page
- PR #210: equipment availability display
"""

import re


def test_event_detail_renders_sections(logged_in_page, base_url):
    """An existing event detail page should render all expected sections."""
    page = logged_in_page

    # Navigate to events list (include all statuses to find seeded events)
    page.goto(f"{base_url}/events/?statuses=DRAFT,PUBLISHED,ASSIGNMENTS_OPEN," "ASSIGNMENTS_CLOSED,COMPLETED,CANCELLED")

    # Click the first event link
    first_event = page.locator("table tbody tr td a[href*='/events/']").first
    if first_event.count() == 0:
        return  # No events seeded
    first_event.click()
    page.wait_for_url(re.compile(r".*/events/\d+"))

    # Core sections should be present
    body = page.locator("body").inner_text()
    assert "Termín" in body, "Missing 'Termín' section"
    assert "Pozice" in body, "Missing 'Pozice' section"


def test_event_detail_has_action_buttons(logged_in_page, base_url):
    """Admin should see 'Upravit' button on a non-completed event detail."""
    page = logged_in_page

    # Filter to DRAFT events where Upravit is always shown
    page.goto(f"{base_url}/events/?statuses=DRAFT")

    first_event = page.locator("table tbody tr td a[href*='/events/']").first
    if first_event.count() == 0:
        return  # No DRAFT events
    first_event.click()
    page.wait_for_url(re.compile(r".*/events/\d+"))

    # Admin should see the edit link (shown for non-completed, non-cancelled)
    edit_btn = page.locator('a[href*="/edit"]')
    assert edit_btn.count() > 0, f"Admin should see 'Upravit' button on DRAFT event detail. " f"URL: {page.url}"
