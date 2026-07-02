// Accessible "Cuenta" dropdown: click/keyboard toggle + aria-expanded + Escape
// + click-outside. CSS still opens it on hover/focus-within for mouse users; this
// adds proper keyboard/touch/AT support (the button was previously a no-op).
(function () {
  document.querySelectorAll('.acct-menu').forEach(function (menu) {
    var btn = menu.querySelector('.acct-btn');
    if (!btn) return;
    btn.setAttribute('aria-expanded', 'false');
    btn.setAttribute('aria-controls', menu.querySelector('.acct-list') ? 'acct-list' : '');
    function close() {
      menu.classList.remove('open');
      btn.setAttribute('aria-expanded', 'false');
    }
    btn.addEventListener('click', function (e) {
      e.preventDefault();
      var open = menu.classList.toggle('open');
      btn.setAttribute('aria-expanded', open ? 'true' : 'false');
    });
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && menu.classList.contains('open')) { close(); btn.focus(); }
    });
    document.addEventListener('click', function (e) {
      if (menu.classList.contains('open') && !menu.contains(e.target)) close();
    });
  });
})();
