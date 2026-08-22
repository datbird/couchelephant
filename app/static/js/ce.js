/* Shared helpers. Every template used to carry its own copy of these; esc()
   alone existed four times, which is four places an escaping regression can
   start. */
(function (w) {
  'use strict';

  function esc(t) {
    return String(t == null ? '' : t).replace(/[&<>"]/g, function (c) {
      return {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[c];
    });
  }

  function coarse() {
    return !!(w.matchMedia && w.matchMedia('(pointer:coarse)').matches);
  }

  function fmtTime(ts) {
    var d = new Date(ts * 1000), h = d.getHours() % 12 || 12, m = d.getMinutes();
    return h + ':' + (m < 10 ? '0' + m : m) + ' ' + (d.getHours() < 12 ? 'AM' : 'PM');
  }

  function fmtWhen(ts) {
    var d = new Date(ts * 1000);
    return d.toLocaleDateString(undefined,
      {weekday: 'short', month: 'short', day: 'numeric'}) + ', ' + fmtTime(ts);
  }

  function fmtDay(ts) {
    var d = new Date(ts * 1000), t = new Date();
    var same = function (a, b) { return a.toDateString() === b.toDateString(); };
    var tm = new Date(t.getTime() + 86400000);
    if (same(d, t)) return 'Today';
    if (same(d, tm)) return 'Tomorrow';
    return d.toLocaleDateString(undefined,
      {weekday: 'long', month: 'long', day: 'numeric'});
  }

  /* Read a JSON reply without dying on one that is not JSON. A proxy error
     page or a traceback would otherwise be swallowed by the parser and the
     user would see nothing at all. */
  function readJson(r) {
    return r.text().then(function (t) {
      try {
        return JSON.parse(t);
      } catch (e) {
        return {ok: false, error: 'HTTP ' + r.status + ': ' + t.slice(0, 200)};
      }
    });
  }

  var KIND_ICON = {
    sports: '<span class="kind sports" title="From a sports pass">' +
      '<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor"' +
      ' stroke-width="2" stroke-linecap="round" stroke-linejoin="round">' +
      '<path d="M7 4h10v5a5 5 0 0 1-10 0V4z"/>' +
      '<path d="M7 6H4v1a4 4 0 0 0 3.4 3.9M17 6h3v1a4 4 0 0 1-3.4 3.9"/>' +
      '<path d="M12 14v3M9 20h6M10 17h4l.5 3h-5z"/></svg></span>',
    series: '<span class="kind series" title="A programme"><svg viewBox="0 0 24 24"' +
      ' width="15" height="15" fill="none" stroke="currentColor" stroke-width="2"' +
      ' stroke-linecap="round" stroke-linejoin="round"><rect x="2.5" y="6" width="19"' +
      ' height="13" rx="2"/><path d="M8 2.8L12 6l4-3.2"/></svg></span>',
    one: '<span class="kind series" title="One broadcast"><svg viewBox="0 0 24 24"' +
      ' width="15" height="15" fill="none" stroke="currentColor" stroke-width="2">' +
      '<circle cx="12" cy="12" r="6"/></svg></span>'
  };

  w.CE = {esc: esc, coarse: coarse, fmtTime: fmtTime, fmtWhen: fmtWhen,
          fmtDay: fmtDay, readJson: readJson, KIND_ICON: KIND_ICON};
})(window);
