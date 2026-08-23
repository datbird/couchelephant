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
    smart: '<span class="kind smart" title="From a smart filter"><svg viewBox="0 0 24 24"' +
      ' width="15" height="15" fill="none" stroke="currentColor" stroke-width="2"' +
      ' stroke-linecap="round" stroke-linejoin="round">' +
      '<path d="M3 5h18M6 12h12M10 19h4"/></svg></span>',
    one: '<span class="kind series" title="One broadcast"><svg viewBox="0 0 24 24"' +
      ' width="15" height="15" fill="none" stroke="currentColor" stroke-width="2">' +
      '<circle cx="12" cy="12" r="6"/></svg></span>'
  };

  /* One option row, and one Plex setting rendered into one.

     These lived twice, in the guide's record panel and in the add panel, which
     is how the record panel gained Plex's own explanations and the add panel
     did not. One copy now. */
  /* The explanation is a tooltip, not a second line.

     Inline, Plex's own summaries were as long as twenty lines. "Detect
     commercials" alone pushed one row past six hundred pixels and shoved
     everything else off the panel. The words are worth keeping; the room
     they took was not. */
  function optRow(owner, id, label, control, hint, cls) {
    return '<label class="optrow' + (cls ? ' ' + cls : '') + '" for="' + id + '">' +
      '<i class="own ' + owner + '" title="' +
        (owner === 'ce' ? 'CouchElephant feature' : 'Plex DVR feature') + '"></i>' +
      '<span class="optlabel"><span class="optname">' + esc(label) + '</span>' +
        (hint ? '<span class="opthelp" tabindex="0" role="note" aria-label="' +
                esc(hint) + '" data-tip="' + esc(hint) + '">?</span>' : '') +
      '</span>' + control + '</label>';
  }

  function settingField(st) {
    var id = 'set_' + st.id, c = '';
    if (st.options && st.options.length) {
      c += '<select id="' + id + '" data-set="' + esc(st.id) + '">';
      st.options.forEach(function (op) {
        c += '<option value="' + esc(op.value) + '"' +
             (String(op.value) === String(st.value) ? ' selected' : '') + '>' +
             esc(op.label) + '</option>';
      });
      c += '</select>';
    } else if (st.type === 'bool') {
      var on = String(st.value) === 'true' || String(st.value) === '1';
      c += '<input type="checkbox" id="' + id + '" data-set="' + esc(st.id) +
           '" data-bool="1"' + (on ? ' checked' : '') + '>';
    } else if (st.type === 'int') {
      /* Suggestions, not limits. Plex sends these as a plain integer with no
         list of allowed values, so the field still takes anything typed in;
         the list is only there so a big number is one click away. */
      var list = (st.presets && st.presets.length) ? id + '_opts' : '';
      c += '<input type="number" min="0" id="' + id + '" data-set="' + esc(st.id) +
           '"' + (list ? ' list="' + list + '"' : '') +
           ' value="' + esc(st.value) + '">';
      if (list) {
        c += '<datalist id="' + list + '">' +
             st.presets.map(function (v) { return '<option value="' + v + '">'; })
               .join('') + '</datalist>';
      }
    } else {
      c += '<input type="text" id="' + id + '" data-set="' + esc(st.id) +
           '" value="' + esc(st.value) + '">';
    }
    /* Plex explains its own settings. Its words, not a second copy of them. */
    return optRow('plex', id, st.label, c, st.hint || '');
  }

  /* One tooltip, on the body, positioned by hand.

     It was a ::after on the mark, which is simpler and wrong: the panel it
     lives in scrolls, and a scrolling ancestor clips anything absolutely
     positioned inside it. The explanation was cut off at the panel edge. A
     fixed element on the body has no such ancestor, and can be clamped to
     the window rather than to whatever box it happens to sit in. */
  var tipEl = null, tipFor = null;

  function tip() {
    if (!tipEl) {
      tipEl = document.createElement('div');
      tipEl.className = 'tipbox';
      tipEl.setAttribute('role', 'tooltip');
      document.body.appendChild(tipEl);
    }
    return tipEl;
  }

  function showTip(mark) {
    var text = mark.getAttribute('data-tip');
    if (!text) return;
    tipFor = mark;
    var el = tip();
    el.textContent = text;
    el.classList.add('on');
    placeTip(mark, el);
  }

  function placeTip(mark, el) {
    var m = mark.getBoundingClientRect();
    var t = el.getBoundingClientRect();
    var pad = 8;
    /* Above by preference, below when there is no room above. */
    var top = m.top - t.height - pad;
    if (top < pad) top = m.bottom + pad;
    var left = m.left + m.width / 2 - t.width / 2;
    /* Clamped to the window, so it is never half off the edge. */
    left = Math.max(pad, Math.min(left, window.innerWidth - t.width - pad));
    el.style.top = Math.round(top) + 'px';
    el.style.left = Math.round(left) + 'px';
  }

  function hideTip() {
    tipFor = null;
    if (tipEl) tipEl.classList.remove('on');
  }

  document.addEventListener('mouseover', function (e) {
    var mark = e.target.closest && e.target.closest('.opthelp');
    if (mark) showTip(mark); else if (!e.target.closest('.tipbox')) hideTip();
  });
  document.addEventListener('focusin', function (e) {
    var mark = e.target.closest && e.target.closest('.opthelp');
    if (mark) showTip(mark); else hideTip();
  });

  /* A panel scrolling under a shown tooltip moves the row it explains, so the
     tooltip follows rather than being dismissed. Hiding was the first attempt
     and it is wrong twice over: pointing at a mark scrolls that mark into
     view, so the tooltip killed itself the moment it appeared. */
  function follow() {
    if (!tipFor || !tipEl) return;
    if (!tipFor.isConnected) { hideTip(); return; }
    placeTip(tipFor, tipEl);
  }
  window.addEventListener('scroll', follow, true);
  window.addEventListener('resize', follow);
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape') hideTip();
  });

  w.CE = {esc: esc, coarse: coarse, fmtTime: fmtTime, fmtWhen: fmtWhen,
          fmtDay: fmtDay, readJson: readJson, KIND_ICON: KIND_ICON,
          optRow: optRow, settingField: settingField,
          showTip: showTip, hideTip: hideTip};
})(window);
