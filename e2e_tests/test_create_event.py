"""E2E tests: create an event and verify it appears in the event list."""


def test_create_event(logged_in_page, base_url):
    """Create a new event via the form and verify it shows up."""
    page = logged_in_page
    event_name = "E2E Test Event"

    page.goto(f"{base_url}/events/create")
    page.wait_for_load_state("networkidle")
    assert page.url.endswith("/events/create") or "/events/create" in page.url

    # Fill mandatory fields
    page.fill("#name", event_name)

    # Event type — select first non-empty option (should default to MEDICAL_COVER)
    page.select_option("#event_type", index=1)

    # Master event — select "Obecné (Výchozí)" (the general/default one)
    me_option = page.locator('#master_event_id option:has-text("Obecné")')
    me_value = me_option.get_attribute("value")
    page.select_option("#master_event_id", value=me_value)

    # Wait for flatpickr to initialize before setting dates
    page.wait_for_function(
        'document.getElementById("start_datetime")._flatpickr !== undefined '
        '&& document.getElementById("end_datetime")._flatpickr !== undefined',
        timeout=15000,
    )

    # Flatpickr datetime fields: altInput=true hides the original <input>.
    # Set the hidden input value directly via JS, then notify flatpickr.
    page.evaluate("""() => {
        const start = document.querySelector('#start_datetime');
        const end = document.querySelector('#end_datetime');
        if (start._flatpickr) start._flatpickr.setDate('2026-12-01 09:00', true);
        if (end._flatpickr) end._flatpickr.setDate('2026-12-01 17:00', true);
    }""")

    # Verify flatpickr values are committed before submitting
    page.wait_for_function(
        'document.getElementById("start_datetime").value !== ""'
        '&& document.getElementById("end_datetime").value !== ""',
        timeout=5000,
    )

    # Submit the form (the "Vytvořit akci" button)
    # Brief pause to let webkit process DOM changes from flatpickr
    page.wait_for_timeout(500)
    page.locator('button[type="submit"][value="create"]').scroll_into_view_if_needed()
    page.locator('button[type="submit"][value="create"]').click()

    # Wait for navigation after form submit; retry click once if webkit doesn't fire
    try:
        page.wait_for_url(lambda url: "/events/" in url and "/events/create" not in url, timeout=5000)
    except Exception:
        # Webkit on Linux sometimes misses the first click
        page.locator('button[type="submit"][value="create"]').click()
        page.wait_for_url(lambda url: "/events/" in url and "/events/create" not in url, timeout=10000)

    # Wait for navigation after form submit
    page.wait_for_load_state("load", timeout=10000)
    page.wait_for_load_state("networkidle")
    # If form validation failed, we're still on /events/create — check for flash errors
    if "/events/create" in page.url:
        flash = page.locator(".alert").all_text_contents()
        invalid_fields = page.evaluate("""() => {
            const fields = document.querySelectorAll('.is-invalid, [aria-invalid="true"]');
            return Array.from(fields).map(f => f.name || f.id || f.className);
        }""")
        form_values = page.evaluate("""() => {
            const fd = new FormData(document.querySelector('form'));
            return Object.fromEntries(fd.entries());
        }""")
        raise AssertionError(
            f"Form submission failed. URL: {page.url}\n"
            f"  Flashes: {flash}\n"
            f"  Invalid fields: {invalid_fields}\n"
            f"  Form values: {form_values}"
        )
    assert "/events/" in page.url
    assert page.locator("h2, h3, h1").filter(has_text=event_name).count() > 0

    # The event is created as DRAFT, which is hidden from the default list view.
    # Verify it appears when filtering by all statuses.
    page.goto(f"{base_url}/events/?statuses=DRAFT")
    assert page.locator(f"text={event_name}").count() > 0
