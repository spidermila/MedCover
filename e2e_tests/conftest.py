"""Playwright E2E test fixtures."""
import os

import pytest

BASE_URL = os.environ.get("BASE_URL", "http://localhost:5000")

# Dev-seed credentials (scripts/seed_dev.py + app/routes/dev.py)
ADMIN_EMAIL = "dev.admin@medcover.local"
ADMIN_PASSWORD = "devpassword"


@pytest.fixture(scope="session")
def base_url() -> str:
    """Base URL of the running Flask app."""
    return BASE_URL


@pytest.fixture()
def logged_in_page(page, base_url):
    """A Playwright page already logged in as admin."""
    _login(page, base_url, ADMIN_EMAIL, ADMIN_PASSWORD)
    yield page


def _login(page, base_url: str, email: str, password: str) -> None:
    """Log into the app via the login form."""
    page.goto(f"{base_url}/auth/login")
    page.fill("#email", email)
    page.fill("#password", password)
    page.locator('button[type="submit"]').click(timeout=60000)
    # Wait for redirect to dashboard
    page.wait_for_url("**/dashboard**", timeout=60000)
