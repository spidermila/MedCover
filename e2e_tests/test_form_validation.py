"""E2E tests: form validation behaviour (live blur validation, cross-field checks).

Regression coverage for:
- PR #220: live on-blur field validation
- PR #165: premature green coloring on valid fields
- PR #182: login form UX
"""


def test_required_fields_show_error_on_blur(logged_in_page, base_url):
    """Leaving a required field empty and tabbing away shows an error."""
    page = logged_in_page
    page.goto(f"{base_url}/events/create")

    # Focus then blur the name field without entering anything
    page.focus("#name")
    page.locator("#event_type").focus()
    page.wait_for_timeout(200)

    assert page.locator("#name.is-invalid").count() > 0


def test_error_clears_when_value_entered(logged_in_page, base_url):
    """Once a required field gets a value, the error should clear."""
    page = logged_in_page
    page.goto(f"{base_url}/events/create")

    # Trigger error
    page.focus("#name")
    page.locator("#event_type").focus()
    page.wait_for_timeout(200)
    assert page.locator("#name.is-invalid").count() > 0

    # Fix it
    page.fill("#name", "Test Event")
    page.wait_for_timeout(200)
    assert page.locator("#name.is-invalid").count() == 0


def test_valid_fields_stay_neutral_before_interaction(logged_in_page, base_url):
    """Fields should not be green before the user interacts with them (PR #165)."""
    page = logged_in_page
    page.goto(f"{base_url}/events/create")

    # Before any interaction, no field should be is-valid or is-invalid
    assert page.locator("#name.is-valid").count() == 0
    assert page.locator("#name.is-invalid").count() == 0
    assert page.locator("#event_type.is-valid").count() == 0


def test_select_validates_on_change(logged_in_page, base_url):
    """Required <select> fields validate when changed (not just on blur)."""
    page = logged_in_page
    page.goto(f"{base_url}/events/create")

    # Select a valid option
    page.select_option("#event_type", index=1)
    page.wait_for_timeout(200)

    # Should not be invalid
    assert page.locator("#event_type.is-invalid").count() == 0


def test_password_mismatch_on_profile(logged_in_page, base_url):
    """Password confirmation mismatch shows an error on the profile page."""
    page = logged_in_page
    page.goto(f"{base_url}/users/profile")

    page.fill("#new_password", "newpassword123")
    page.fill("#confirm_password", "differentpassword")
    page.locator("#new_password").focus()
    page.wait_for_timeout(300)

    assert page.locator("#confirm_password.is-invalid").count() > 0
