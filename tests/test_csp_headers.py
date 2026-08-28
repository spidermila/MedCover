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

    def test_meta_csp_nonce_matches_header(self, prod_client):
        # A public page that extends base.html is enough. The login page fits
        # (no auth needed, real HTML body, extends base.html).
        resp = prod_client.get("/auth/login")
        assert resp.status_code == 200
        assert "Content-Security-Policy" in resp.headers

        csp = resp.headers["Content-Security-Policy"]
        m = re.search(r"style-src[^;]*'nonce-([0-9a-f]+)'", csp)
        assert m, csp
        style_nonce = m.group(1)

        body = resp.data.decode()
        meta_match = re.search(r'<meta\s+name="csp-nonce"\s+content="([0-9a-f]+)"', body)
        assert meta_match, (
            'base.html must render <meta name="csp-nonce"> so libraries like '
            "FullCalendar can nonce runtime-injected <style> tags"
        )
        assert meta_match.group(1) == style_nonce, "meta csp-nonce must match the nonce in the CSP header"


# ── Static template audit ─────────────────────────────────────────────────────


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
