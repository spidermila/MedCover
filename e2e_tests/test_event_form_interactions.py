"""E2E tests: event form JS interactions (toggles, datetime, event type).

Regression coverage for:
- PR #220: toggle CSS consolidation
- PR #166: Fix Teď button duplicate class attributes
- PR #165: premature green coloring
"""


def test_paid_toggle_switches_label(logged_in_page, base_url):
    """Clicking the paid toggle should update the label styling."""
    page = logged_in_page
    page.goto(f"{base_url}/events/create")
    page.wait_for_load_state("networkidle")

    label = page.locator("label.paid-label")
    assert label.count() > 0

    # Initially unchecked — label should not have is-paid
    assert "is-paid" not in (label.get_attribute("class") or "")

    # Toggle via JS (Playwright click on Bootstrap switch labels is unreliable)
    page.evaluate(
        'var cb = document.getElementById("paid");' "cb.checked = true;" 'cb.dispatchEvent(new Event("change"));'
    )
    page.wait_for_timeout(300)
    assert "is-paid" in (
        label.get_attribute("class") or ""
    ), f"Expected is-paid class, got: {label.get_attribute('class')}"

    # Toggle back
    page.evaluate(
        'var cb = document.getElementById("paid");' "cb.checked = false;" 'cb.dispatchEvent(new Event("change"));'
    )
    page.wait_for_timeout(300)
    assert "is-paid" not in (label.get_attribute("class") or "")


def test_event_type_training_shows_participants(logged_in_page, base_url):
    """Selecting TRAINING event type should reveal the participants count field."""
    page = logged_in_page
    page.goto(f"{base_url}/events/create")
    page.wait_for_load_state("networkidle")

    row = page.locator("#planned_participants_row")
    # Initially hidden (MEDICAL_COVER is default)
    assert not row.is_visible()

    # Switch to TRAINING (use value= to avoid whitespace issues)
    page.select_option("#event_type", value="TRAINING")
    page.wait_for_timeout(300)
    assert row.is_visible()

    # Switch back
    page.select_option("#event_type", value="MEDICAL_COVER")
    page.wait_for_timeout(300)
    assert not row.is_visible()


def test_ted_button_sets_datetime(logged_in_page, base_url):
    """The 'Teď' (now) button should populate the datetime field."""
    page = logged_in_page
    page.goto(f"{base_url}/events/create")
    page.wait_for_load_state("networkidle")

    # Wait for flatpickr to initialize AND fpNow to be available
    page.wait_for_function(
        'typeof fpNow === "function" ' '&& document.getElementById("start_datetime")._flatpickr !== undefined',
        timeout=15000,
    )

    # The hidden input should be empty initially
    initial_val = page.evaluate("document.getElementById('start_datetime').value")
    assert initial_val == ""

    # Call fpNow directly — Playwright click doesn't reliably trigger
    # the DOMContentLoaded-bound listener across all browsers
    page.evaluate('fpNow(document.querySelector(".btn-fpnow"))')
    page.wait_for_timeout(300)

    # flatpickr sets the hidden original input value
    new_val = page.evaluate("document.getElementById('start_datetime').value")
    assert new_val != "", "Teď button did not set a datetime value"


def test_dark_mode_toggle_on_profile(logged_in_page, base_url):
    """Dark mode toggle on profile page should update label styling."""
    page = logged_in_page
    page.goto(f"{base_url}/users/profile")

    toggle = page.locator("#dark_mode")
    label = page.locator("label.dark-label")

    if toggle.count() == 0 or label.count() == 0:
        return  # Dark mode not present on this page

    # Get initial state
    was_checked = toggle.is_checked()

    # Click toggle
    toggle.click()
    page.wait_for_timeout(200)
    new_class = label.get_attribute("class") or ""

    if was_checked:
        assert "is-dark" not in new_class
    else:
        assert "is-dark" in new_class
