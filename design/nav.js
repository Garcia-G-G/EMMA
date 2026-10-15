/* Emma shared nav + footer. Mirrored like tokens.css.
   A page carries <header data-nav></header> and <footer data-footer></footer>;
   this fills them. Language comes from <html lang>; the other language's URL from
   <link rel="alternate" hreflang>. Signed-in state from /api/auth/whoami (same host).
   ROUTES must match backend/pages.py:ROUTES (tests/test_design_sync.py checks). */
(function () {
  var ROUTES = {
    home: { es: '/', en: '/en/' },
    pricing: { es: '/precios', en: '/en/pricing' },
    install: { es: '/instalar', en: '/en/install' },
    login: { es: '/entrar', en: '/en/login' },
    account: { es: '/cuenta', en: '/en/account' },
    privacy: { es: '/privacidad', en: '/en/privacy' },
    terms: { es: '/terminos', en: '/en/terms' }
  };
  var T = {
    es: { pricing: 'Precios', install: 'Instalar', login: 'Entrar', account: 'Cuenta',
          privacy: 'Privacidad', terms: 'Términos', skip: 'Saltar al contenido', home: 'Emma, inicio', lang: 'Idioma' },
    en: { pricing: 'Pricing', install: 'Install', login: 'Sign in', account: 'Account',
          privacy: 'Privacy', terms: 'Terms', skip: 'Skip to content', home: 'Emma, home', lang: 'Language' }
  };
  var ATOM = '<svg class="atom" viewBox="0 0 100 100" aria-hidden="true" focusable="false">' +
    '<g transform="translate(50 50)"><ellipse class="ring" rx="42" ry="14"/>' +
    '<ellipse class="ring" rx="42" ry="14" transform="rotate(60)"/>' +
    '<ellipse class="ring" rx="42" ry="14" transform="rotate(-60)"/></g>' +
    '<circle class="nucleus" cx="50" cy="50" r="11" opacity="0.55"/><circle class="core" cx="50" cy="50" r="6"/></svg>';

  var lang = (document.documentElement.lang || 'es').slice(0, 2) === 'en' ? 'en' : 'es';
  var t = T[lang];
  var path = location.pathname.replace(/\/+$/, '') || '/';
  function href(key) { return ROUTES[key][lang]; }
  function link(key, cls) {
    var h = href(key), cur = (h.replace(/\/+$/, '') || '/') === path;
    return '<a href="' + h + '"' + (cls ? ' class="' + cls + '"' : '') + (cur ? ' aria-current="page"' : '') + '>' + t[key] + '</a>';
  }
  function alt(l) {
    var el = document.querySelector('link[rel="alternate"][hreflang="' + l + '"]');
    return el ? new URL(el.href).pathname : ROUTES.home[l];
  }

  var nav = document.querySelector('[data-nav]');
  if (nav) {
    nav.innerHTML =
      '<a class="skip-link" href="#main">' + t.skip + '</a>' +
      '<nav class="nav" aria-label="Emma">' +
      '<a class="nav-brand" href="' + href('home') + '" aria-label="' + t.home + '">' + ATOM + '<span class="nm">Emma</span></a>' +
      '<div class="nav-links">' + link('pricing', 'hide-sm') + link('install', 'hide-sm') +
      '<span data-auth>' + link('login') + '</span>' +
      '<span class="lang-toggle" role="group" aria-label="' + t.lang + '">' +
      '<a href="' + alt('es') + '" hreflang="es" lang="es"' + (lang === 'es' ? ' aria-current="true"' : '') + '>ES</a>' +
      '<span class="sep" aria-hidden="true">/</span>' +
      '<a href="' + alt('en') + '" hreflang="en" lang="en"' + (lang === 'en' ? ' aria-current="true"' : '') + '>EN</a>' +
      '</span></div></nav>';
    // Remember an explicit language choice; the landing reads it on "/".
    nav.querySelectorAll('.lang-toggle a').forEach(function (a) {
      a.addEventListener('click', function () { try { localStorage.setItem('lang', a.getAttribute('hreflang')); } catch (e) {} });
    });
    fetch('/api/auth/whoami', { credentials: 'same-origin' })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) {
        if (d && d.authenticated) nav.querySelector('[data-auth]').innerHTML = link('account');
      })
      .catch(function () {});
  }

  var foot = document.querySelector('[data-footer]');
  if (foot) {
    foot.className = 'footer';
    foot.innerHTML =
      '<span class="footer-brand">' + ATOM + '© 2026 Emma · The Emma Family</span>' +
      '<span class="footer-links">' + link('privacy') + link('terms') + link('install') + '</span>';
  }
})();
