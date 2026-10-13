// Page texts in the rider's language. The server fills in @@LANGUAGES@@ with every
// lang/<code>.json file; each holds "key": "text" pairs, where {name} is replaced
// by a parameter. A missing text falls back to Danish, then to the key itself.
(function () {
  var LANGUAGES = @@LANGUAGES@@;
  var FALLBACK = 'da';
  var STORAGE_KEY = 'pyergometer-language';

  function chooseLanguage() {
    var saved = null;
    try {
      saved = window.localStorage.getItem(STORAGE_KEY);
    } catch (error) { /* optional */ }
    if (saved && LANGUAGES[saved]) return saved;
    // First visit on this device: the browser's own language, if there is a file for it.
    var wanted = navigator.languages || [navigator.language || ''];
    for (var index = 0; index < wanted.length; index += 1) {
      var code = String(wanted[index]).toLowerCase().split('-')[0];
      if (LANGUAGES[code]) return code;
    }
    return LANGUAGES[FALLBACK] ? FALLBACK : Object.keys(LANGUAGES)[0];
  }

  var language = chooseLanguage();
  document.documentElement.lang = language;

  function lookup(key) {
    var texts = LANGUAGES[language] || {};
    if (texts[key] !== undefined) return texts[key];
    var fallback = LANGUAGES[FALLBACK] || {};
    return fallback[key];
  }

  function t(key, params) {
    var text = lookup(key);
    if (text === undefined) return key;
    if (typeof text !== 'string' || !params) return text;
    return text.replace(/\{(\w+)\}/g, function (match, name) {
      var value = params[name];
      return value === undefined || value === null ? match : String(value);
    });
  }

  // Decimal numbers with the language's decimal sign and no thousands separator.
  function number(value, decimals) {
    var options = {useGrouping: false};
    if (decimals === undefined) {
      options.maximumFractionDigits = 10;
    } else {
      options.minimumFractionDigits = decimals;
      options.maximumFractionDigits = decimals;
    }
    return new Intl.NumberFormat(language, options).format(value);
  }

  // A fitness-test problem from the server: {code, ...parameters}. History saved
  // before codes existed holds the Danish sentence itself.
  function problem(item) {
    return typeof item === 'string' ? item : t('problem.' + item.code, item);
  }

  // An error reply from the server, in the rider's language when it has a code.
  function serverError(data, fallbackKey) {
    if (data && data.code && lookup('server.' + data.code) !== undefined) {
      return t('server.' + data.code, data.params || {});
    }
    return (data && data.error) || t(fallbackKey);
  }

  // Fills in the static page: data-i18n sets the text, data-i18n-attr sets
  // attributes as "attribute:key;attribute:key".
  function apply(root) {
    root = root || document;
    Array.prototype.forEach.call(root.querySelectorAll('[data-i18n]'), function (element) {
      element.textContent = t(element.getAttribute('data-i18n'));
    });
    Array.prototype.forEach.call(root.querySelectorAll('[data-i18n-attr]'), function (element) {
      element.getAttribute('data-i18n-attr').split(';').forEach(function (pair) {
        var parts = pair.split(':');
        if (parts.length === 2) element.setAttribute(parts[0].trim(), t(parts[1].trim()));
      });
    });
  }

  function setLanguage(code) {
    if (!LANGUAGES[code]) return;
    try {
      window.localStorage.setItem(STORAGE_KEY, code);
    } catch (error) { /* the choice then lasts until the page is reloaded */ }
    window.location.reload();
  }

  // Language codes with the language's own name, for a language picker.
  function languages() {
    return Object.keys(LANGUAGES).sort().map(function (code) {
      return {code: code, name: LANGUAGES[code]['language.name'] || code};
    });
  }

  window.t = t;
  window.I18N = {
    language: language,
    t: t,
    number: number,
    problem: problem,
    serverError: serverError,
    apply: apply,
    setLanguage: setLanguage,
    languages: languages
  };
}());
