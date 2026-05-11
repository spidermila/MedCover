/* Invite list page helpers */

function copyInviteUrl(btn) {
  const url = btn.dataset.inviteUrl;
  navigator.clipboard.writeText(url).then(function () {
    const original = btn.textContent;
    btn.textContent = "✓ Zkopírováno";
    setTimeout(function () {
      btn.textContent = original;
    }, 2000);
  });
}

document.addEventListener("DOMContentLoaded", function () {
  document.querySelectorAll(".copy-invite-btn").forEach(function (btn) {
    btn.addEventListener("click", function () { copyInviteUrl(btn); });
  });
});
