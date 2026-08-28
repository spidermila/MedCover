"""Content-Security-Policy header + inline-CSS regression tests.

The app serves a strict CSP that forbids `'unsafe-inline'` in `style-src`;
the policy relies on per-request nonces for the small handful of `<style>`
blocks and third-party libraries (e.g. FullCalendar) that need runtime styles.

Two protections here:

1. Dynamic checks: exercise the response and confirm the header uses a nonce
   and the same nonce is embedded in `<style>` / `<script>` / `<meta
   name="csp-nonce">` tags rendered on the page.
2. Static checks: walk every browser-facing template and fail on any inline
   `style="..."` attribute or unnonced `<style>` block. Email templates
   (rendered into email HTML, never served with a CSP header) are exempt.
"""

import re
from pathlib import Path

import pytest

from app.models.role import Role
from tests.conftest import _login, _make_user

TEMPLATE_DIR = Path(__file__).parent.parent / "app" / "templates"

# Directories whose HTML is rendered into email bodies, not served with a CSP.
EMAIL_TEMPLATE_DIRS = {"email"}


def _iter_browser_templates():
    for tmpl in sorted(TEMPLATE_DIR.rglob("*.html")):
        rel = tmpl.relative_to(TEMPLATE_DIR)
        if rel.parts and rel.parts[0] in EMAIL_TEMPLATE_DIRS:
            continue
        yield tmpl


# ── Dynamic header checks ─────────────────────────────────────────────────────


@pytest.fixture
def prod_client(app):
    """Client that sees production security headers.

    The CSP header is only emitted when neither TESTING nor DEBUG is on.
    Flip both off around the request so we can assert on the header.
    """
    orig_testing = app.config.get("TESTING")
    orig_debug = app.config.get("DEBUG")
    app.config["TESTING"] = False
    app.config["DEBUG"] = False
    try:
        yield app.test_client()
    finally:
        app.config["TESTING"] = orig_testing
        app.config["DEBUG"] = orig_debug


class TestCspHeader:
    def test_csp_header_present_in_prod_mode(self, prod_client):
        resp = prod_client.get("/health")
        assert "Content-Security-Policy" in resp.headers

    def test_style_src_uses_nonce_not_unsafe_inline(self, prod_client):
        """style-src must be nonce-based; 'unsafe-inline' would defeat CSP."""
        resp = prod_client.get("/health")
        csp = resp.headers["Content-Security-Policy"]
        m = re.search(r"style-src\s+([^;]+)", csp)
        assert m, f"style-src directive missing from CSP: {csp}"
        style_src = m.group(1)
        assert "'unsafe-inline'" not in style_src, f"style-src must not contain 'unsafe-inline'; got: {style_src}"
        assert re.search(
            r"'nonce-[0-9a-f]{16,}'", style_src
        ), f"style-src must include a per-request nonce; got: {style_src}"

    def test_script_src_still_uses_nonce(self, prod_client):
        """Regression: script-src nonce must not be lost while reworking style-src."""
        resp = prod_client.get("/health")
        csp = resp.headers["Content-Security-Policy"]
        m = re.search(r"script-src\s+([^;]+)", csp)
        assert m, f"script-src directive missing from CSP: {csp}"
        assert "'unsafe-inline'" not in m.group(1)
        assert re.search(r"'nonce-[0-9a-f]{16,}'", m.group(1))

    def test_csp_nonce_is_fresh_per_request(self, prod_client):
        """Two requests must receive different nonces."""

        def _extract_style_nonce(csp: str) -> str:
            m = re.search(r"style-src[^;]*'nonce-([0-9a-f]+)'", csp)
            assert m, csp
            return m.group(1)

        r1 = prod_client.get("/health")
        r2 = prod_client.get("/health")
        assert _extract_style_nonce(r1.headers["Content-Security-Policy"]) != _extract_style_nonce(
            r2.headers["Content-Security-Policy"]
        )


class TestCspNonceInMarkup:
    """The nonce advertised in the CSP header must match what templates emit."""

    # Routes exercised for nonce-in-markup verification. Cover several
    # blueprints and both authenticated / unauthenticated paths, so that
    # dropping <meta name="csp-nonce"> from base.html can only be missed if
    # every listed route is refactored away simultaneously.
    NONCE_MARKUP_ROUTES = [
        ("/auth/login", False),
        ("/", True),
        ("/events/", True),
        ("/users/profile", True),
    ]

    @pytest.mark.parametrize("path,needs_login", NONCE_MARKUP_ROUTES)
    def test_meta_csp_nonce_matches_header(self, app, path, needs_login):
        """Every browser-facing page must render <meta name="csp-nonce">
        whose value equals the nonce in the CSP header."""
        client = app.test_client()
        if needs_login:
            with app.app_context():
                _make_user("nonce-test@test.com", "Nonce Test", Role.ADMIN)
            _login(client, "nonce-test@test.com")

        # Flip out of testing/debug so the CSP header is emitted.
        orig_testing = app.config.get("TESTING")
        orig_debug = app.config.get("DEBUG")
        app.config["TESTING"] = False
        app.config["DEBUG"] = False
        try:
            resp = client.get(path, follow_redirects=True)
        finally:
            app.config["TESTING"] = orig_testing
            app.config["DEBUG"] = orig_debug

        assert resp.status_code == 200, f"{path} returned {resp.status_code}"
        assert "Content-Security-Policy" in resp.headers, path

        csp = resp.headers["Content-Security-Policy"]
        m = re.search(r"style-src[^;]*'nonce-([0-9a-f]+)'", csp)
        assert m, csp
        style_nonce = m.group(1)

        body = resp.data.decode()
        meta_match = re.search(r'<meta\s+name="csp-nonce"\s+content="([0-9a-f]+)"', body)
        assert meta_match, (
            f'{path}: base.html must render <meta name="csp-nonce"> so libraries like '
            "FullCalendar can nonce runtime-injected <style> tags"
        )
        assert meta_match.group(1) == style_nonce, f"{path}: meta csp-nonce must match the nonce in the CSP header"


# ── Static template audit ─────────────────────────────────────────────────────


class TestThirdPartyStyleInjection:
    """Third-party libraries that inject <style> at runtime must be handled
    so their inserts pass the nonce-only style-src. Two mechanisms cover
    every case we know of: FullCalendar reads <meta name="csp-nonce">, and
    the base.html document.createElement shim auto-nonces every <style>
    element created by anything else (e.g. Flatpickr)."""

    def test_bundled_fullcalendar_reads_csp_nonce_meta(self):
        """FullCalendar's bundle must query meta[name="csp-nonce"] and apply
        the nonce to the <style> element it creates. If a future FullCalendar
        upgrade drops this discovery path, the runtime styles will be blocked
        and the calendar will render broken."""
        bundle = Path(__file__).parent.parent / "app" / "static" / "js" / "fullcalendar.min.js"
        assert bundle.exists(), f"FullCalendar bundle not found at {bundle}"
        content = bundle.read_text()
        assert 'meta[name="csp-nonce"]' in content, (
            'FullCalendar bundle no longer reads <meta name="csp-nonce">. '
            "Its runtime <style> injection will be blocked by the CSP."
        )
        assert ".nonce=" in content, (
            "FullCalendar bundle no longer applies a discovered nonce to its " "created <style> elements."
        )

    def test_base_html_has_create_element_nonce_shim(self):
        """Regression guard: the shim in base.html is what covers libraries
        that do not consult the meta tag (Flatpickr, etc.). Losing it would
        silently break every third-party runtime style outside FullCalendar."""
        base = TEMPLATE_DIR / "base.html"
        content = base.read_text()
        assert "document.createElement = function" in content, (
            "base.html no longer wraps document.createElement to auto-nonce "
            "runtime <style> elements. Libraries like Flatpickr will break."
        )
        # The shim must run before any third-party <script> loads; cheapest
        # check is that it sits inside <head>.
        head_close = content.lower().find("</head>")
        shim_pos = content.find("document.createElement = function")
        assert 0 < shim_pos < head_close, (
            "createElement shim must live inside <head> so it runs before " "any third-party JS loaded from <body>."
        )


class TestNoInlineStylesInBrowserTemplates:
    """Regression guard: reintroducing inline styles would silently break
    with the strict CSP because 'unsafe-inline' is gone from style-src."""

    _STYLE_ATTR_RE = re.compile(r'\bstyle\s*=\s*["\']', re.IGNORECASE)
    _STYLE_TAG_RE = re.compile(r"<style\b([^>]*)>", re.IGNORECASE)
    _NONCE_ATTR_RE = re.compile(r"\bnonce\s*=", re.IGNORECASE)
    # Strip Jinja {# ... #} comments and HTML <!-- ... --> comments before
    # scanning: any `style=` or `<style>` mentioned inside a comment (e.g. in
    # a docstring above a shim) is documentation, not markup.
    _JINJA_COMMENT_RE = re.compile(r"\{#.*?#\}", re.DOTALL)
    _HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)

    def _strip_comments(self, content: str) -> str:
        content = self._JINJA_COMMENT_RE.sub("", content)
        content = self._HTML_COMMENT_RE.sub("", content)
        return content

    def test_no_style_attributes(self):
        """No `style="..."` attributes in any browser template. Use a class
        in main.css (or add a new utility) instead — dynamic values that
        depend on request data should live in JS via data-* attributes."""
        offenders: list[str] = []
        for tmpl in _iter_browser_templates():
            content = self._strip_comments(tmpl.read_text())
            for lineno, line in enumerate(content.splitlines(), start=1):
                if self._STYLE_ATTR_RE.search(line):
                    offenders.append(f"{tmpl.relative_to(TEMPLATE_DIR)}:{lineno}: {line.strip()}")
        assert not offenders, (
            "Inline style attributes found in browser templates (CSP forbids "
            "'unsafe-inline' in style-src). Move the CSS to app/static/css/main.css.\n"
            + "\n".join(f"  {o}" for o in offenders)
        )

    def test_all_style_tags_are_nonced(self):
        """Any remaining <style> block in a browser template must carry
        `nonce="{{ g.csp_nonce }}"` so it validates under the CSP."""
        offenders: list[str] = []
        for tmpl in _iter_browser_templates():
            content = self._strip_comments(tmpl.read_text())
            for m in self._STYLE_TAG_RE.finditer(content):
                if not self._NONCE_ATTR_RE.search(m.group(1)):
                    lineno = content.count("\n", 0, m.start()) + 1
                    offenders.append(f"{tmpl.relative_to(TEMPLATE_DIR)}:{lineno}: {m.group(0)}")
        assert not offenders, (
            "Unnonced <style> blocks found in browser templates. Either move "
            'the CSS to main.css or add nonce="{{ g.csp_nonce }}".\n' + "\n".join(f"  {o}" for o in offenders)
        )
