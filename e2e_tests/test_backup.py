"""E2E tests: backup creation and restore (stored file + upload)."""

import tempfile
from pathlib import Path


def _run_backup(page, base_url: str) -> str:
    """Navigate to the backup page, create a backup, and return its filename."""
    page.goto(f"{base_url}/admin/backup/")
    page.wait_for_load_state("networkidle")
    page.locator('button:has-text("Vytvořit zálohu nyní")').click()
    page.wait_for_load_state("networkidle")
    # The newest backup is listed first; grab its filename from the download link.
    href = page.locator('a:has-text("Stáhnout")').first.get_attribute("href")
    assert href, "No backup download link found after running backup"
    return href.split("/")[-1]


def _create_master_event(page, base_url: str, name: str) -> None:
    """Create a master event via the UI."""
    page.goto(f"{base_url}/master-events/create")
    page.wait_for_load_state("domcontentloaded")
    page.fill("#name", name)
    page.locator('button[type="submit"]').click()
    page.wait_for_load_state("networkidle")


def _me_visible_in_list(page, base_url: str, name: str) -> bool:
    page.goto(f"{base_url}/master-events/")
    page.wait_for_load_state("networkidle")
    return page.locator(f"text={name}").count() > 0


def test_backup_and_restore_from_stored_file(logged_in_page, base_url):
    """Full cycle: backup → create ME → restore stored backup → ME disappears."""
    page = logged_in_page
    me_name = "E2E Stored Restore Test ME"

    # 1. Create a backup of the current state (without the new ME).
    filename = _run_backup(page, base_url)

    # 2. Create a master event that should NOT survive the restore.
    _create_master_event(page, base_url, me_name)
    assert _me_visible_in_list(page, base_url, me_name), "ME should exist before restore"

    # 3. Go to backup page and click the Restore button for our backup.
    page.goto(f"{base_url}/admin/backup/")
    page.wait_for_load_state("networkidle")

    # Click the "Obnovit" button with matching data-filename — opens the modal.
    page.locator(f'button[data-filename="{filename}"][data-bs-target="#restoreModal"]').click()

    # Wait for the Bootstrap modal to become visible.
    page.locator("#restoreModal").wait_for(state="visible", timeout=5000)

    # Fill the confirmation word and submit.
    page.locator("#restore_confirmation").fill("RESTORE")
    with page.expect_navigation(timeout=60000):
        page.locator("#restoreModal button[type='submit']").click()
    page.wait_for_load_state("networkidle")

    # 4. Verify the ME created after the backup is gone.
    assert not _me_visible_in_list(page, base_url, me_name), "ME created after backup should not exist after restore"


def test_backup_and_restore_via_upload(logged_in_page, base_url):
    """Full cycle: backup → download → create ME → upload-restore → ME disappears."""
    page = logged_in_page
    me_name = "E2E Upload Restore Test ME"

    # 1. Create a backup and download the zip file.
    _run_backup(page, base_url)
    page.goto(f"{base_url}/admin/backup/")
    page.wait_for_load_state("networkidle")

    with page.expect_download() as dl_info:
        page.locator('a:has-text("Stáhnout")').first.click()
    download = dl_info.value

    # Save to a path we control — download.path() points to a Playwright-managed
    # temp file that may be cleaned up as soon as the download object is GC'd.
    tmp_dir = Path(tempfile.mkdtemp())
    backup_file = tmp_dir / "restore_upload.zip"
    download.save_as(str(backup_file))
    assert backup_file.exists(), "Saved backup file must exist"

    # 2. Create a master event that should NOT survive the restore.
    _create_master_event(page, base_url, me_name)
    assert _me_visible_in_list(page, base_url, me_name), "ME should exist before restore"

    # 3. Upload the backup file and restore.
    page.goto(f"{base_url}/admin/backup/")
    page.wait_for_load_state("networkidle")

    page.locator("#backup_file").set_input_files(str(backup_file))
    page.locator("#upload_confirmation").fill("RESTORE")
    # The submit button carries data-confirm which triggers window.confirm().
    # Playwright auto-dismisses confirm() (returns false), blocking the submit.
    # Register a one-time handler to accept the dialog before clicking.
    page.once("dialog", lambda dialog: dialog.accept())
    with page.expect_navigation(timeout=60000):
        page.locator("#uploadRestoreForm button[type='submit']").click()
    page.wait_for_load_state("networkidle")

    # 4. Verify the ME created after the backup is gone.
    assert not _me_visible_in_list(
        page, base_url, me_name
    ), "ME created after backup should not exist after upload-restore"
