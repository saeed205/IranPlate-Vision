/*
 * Tiny bilingual helper.
 *
 * Markup is written as "English | فارسی" and this script picks a side based on
 * localStorage.lang, also setting <html lang/dir>.
 *
 * Added here:
 *   - I18N.onChange(fn): pages register their re-render callbacks, so switching
 *     language also refreshes lists that were built by JS (camera list, access
 *     log, scan history). Before this, those kept the old language until reload.
 *   - I18N.esc()/attr(): HTML escaping, because every list in the app is built
 *     with innerHTML from server data (camera names, plates, labels, notes).
 */
(function () {
  var listeners = [];

  function getLang() {
    try {
      return localStorage.getItem('lang') === 'en' ? 'en' : 'fa';
    } catch (e) {
      return 'fa'; // private mode / storage disabled
    }
  }

  function pick(raw, lang) {
    if (typeof raw !== 'string' || raw.indexOf('|') === -1) return raw;
    var parts = raw.split('|');
    var en = (parts[0] || '').trim();
    var fa = (parts.slice(1).join('|') || '').trim();
    return (lang || getLang()) === 'en' ? en : fa;
  }

  /* Escape text destined for innerHTML. */
  function esc(value) {
    if (value === null || value === undefined) return '';
    return String(value)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  /* Escape text destined for a quoted attribute value. */
  function attr(value) {
    return esc(value).replace(/`/g, '&#96;');
  }

  function apply(root) {
    var lang = getLang();
    var html = document.documentElement;
    html.setAttribute('lang', lang);
    html.setAttribute('dir', lang === 'fa' ? 'rtl' : 'ltr');

    var walker = document.createTreeWalker(root || document, NodeFilter.SHOW_TEXT);
    var node;
    while ((node = walker.nextNode())) {
      var raw = node.__i18nRaw || node.nodeValue;
      if (raw && raw.indexOf('|') !== -1) {
        node.__i18nRaw = raw;
        node.nodeValue = pick(raw, lang);
      }
    }

    var ATTRS = ['title', 'placeholder', 'aria-label'];
    var all = (root || document).querySelectorAll('*');
    for (var i = 0; i < all.length; i++) {
      var el = all[i];
      if (!el.dataset) continue;
      for (var a = 0; a < ATTRS.length; a++) {
        var name = ATTRS[a];
        var key = 'i18n' + name.replace('-', '_');
        var stored = el.dataset[key];
        var current = el.getAttribute(name);
        var value = stored || current;
        if (value && value.indexOf('|') !== -1) {
          if (!stored) el.dataset[key] = value;
          el.setAttribute(name, pick(value, lang));
        }
      }
    }

    var btn = document.getElementById('langToggle');
    if (btn) btn.textContent = lang === 'fa' ? 'EN' : 'FA';
  }

  /* Register a callback that re-renders JS-built content on language change. */
  function onChange(fn) {
    if (typeof fn === 'function') listeners.push(fn);
  }

  function notify() {
    var lang = getLang();
    for (var i = 0; i < listeners.length; i++) {
      try {
        listeners[i](lang);
      } catch (e) {
        console.error('i18n listener failed', e);
      }
    }
  }

  function setLang(lang) {
    try {
      localStorage.setItem('lang', lang === 'en' ? 'en' : 'fa');
    } catch (e) { /* storage unavailable — apply for this page only */ }
    apply(document);
    notify();
  }

  function toggle() {
    setLang(getLang() === 'fa' ? 'en' : 'fa');
  }

  function mount() {
    apply(document);
    var btn = document.getElementById('langToggle');
    if (btn && !btn.__i18nMounted) {
      btn.__i18nMounted = true;
      btn.addEventListener('click', toggle);
    }
    notify();
  }

  window.I18N = {
    getLang: getLang, setLang: setLang, toggle: toggle,
    pick: pick, apply: apply, onChange: onChange, esc: esc, attr: attr
  };
  window.__tPipe = function (s) { return pick(s, getLang()); };
  window.__esc = esc;
  window.__attr = attr;

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', mount);
  } else {
    mount();
  }
})();
