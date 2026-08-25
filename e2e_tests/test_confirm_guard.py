"""E2E tests: guardedConfirm prevents duplicate confirm dialogs on rapid events.

Regression coverage for #449 / PR #450: on some input devices (macOS
trackpads, assistive tech) a single physical click dispatches multiple
click/submit events, each of which used to pop its own native confirm()
dialog. All confirm call sites now route through `guardedConfirm` in
app-init.js, which prompts at most once per element.
"""


def test_guarded_confirm_form_prompts_only_once_on_rapid_submits(logged_in_page, base_url):
    """Rapid submit events on a data-confirm form must trigger confirm() only once."""
    page = logged_in_page
    page.goto(f"{base_url}/users/profile")
    page.wait_for_load_state("domcontentloaded")
    # The calendar regenerate form always renders on the profile page.
    page.wait_for_selector('form[action$="/calendar/regenerate"][data-confirm]', state="attached")

    result = page.evaluate("""() => {
          const form = document.querySelector('form[action$="/calendar/regenerate"][data-confirm]');
          let confirmCount = 0;
          window.confirm = () => { confirmCount += 1; return true; };
          // Block the actual navigation so the test does not leave /users/profile.
          form.addEventListener('submit', (e) => e.preventDefault(), true);
          for (let i = 0; i < 3; i++) {
            form.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));
          }
          return { confirmCount, confirmedFlag: form.dataset.confirmed };
        }""")
    assert result["confirmCount"] == 1, f"confirm() called {result['confirmCount']}× (expected 1)"
    assert result["confirmedFlag"] == "1"


def test_guarded_confirm_form_cancel_burst_prompts_only_once(logged_in_page, base_url):
    """Cancelling the first dialog must still suppress duplicate prompts from the same burst."""
    page = logged_in_page
    page.goto(f"{base_url}/users/profile")
    page.wait_for_load_state("domcontentloaded")
    page.wait_for_selector('form[action$="/calendar/regenerate"][data-confirm]', state="attached")

    result = page.evaluate("""() => {
          const form = document.querySelector('form[action$="/calendar/regenerate"][data-confirm]');
          let confirmCount = 0;
          window.confirm = () => { confirmCount += 1; return false; };
          form.addEventListener('submit', (e) => e.preventDefault(), true);
          for (let i = 0; i < 3; i++) {
            form.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));
          }
          return { confirmCount, confirmedFlag: form.dataset.confirmed };
        }""")
    assert result["confirmCount"] == 1, f"confirm() called {result['confirmCount']}× (expected 1)"
    # Cancel path must not mark the element as permanently confirmed.
    assert result["confirmedFlag"] is None


def test_guarded_confirm_helper_returns_false_on_repeat(logged_in_page, base_url):
    """Direct check of the helper: after acceptance, further calls return false without prompting."""
    page = logged_in_page
    page.goto(f"{base_url}/users/profile")
    page.wait_for_load_state("domcontentloaded")
    page.wait_for_function('typeof guardedConfirm === "function"', timeout=5000)

    result = page.evaluate("""() => {
          const btn = document.createElement('button');
          document.body.appendChild(btn);
          let confirmCount = 0;
          window.confirm = () => { confirmCount += 1; return true; };
          const first = guardedConfirm(btn, 'x');
          const second = guardedConfirm(btn, 'x');
          const third = guardedConfirm(btn, 'x');
          return { confirmCount, first, second, third };
        }""")
    assert result["first"] is True
    assert result["second"] is False
    assert result["third"] is False
    assert result["confirmCount"] == 1
