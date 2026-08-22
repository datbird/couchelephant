/* The "limit to networks or channels" control.

   A dropdown that takes more than one answer. It looks like the single-choice
   ones beside it and opens a list where every item has its own checkbox.

   This existed twice, once in the guide's record panel and once in the add
   panel, and the copies had already drifted: only one skipped autofocus on a
   touch screen. One component now, used by both. */
(function (w) {
  'use strict';
  var esc = w.CE.esc, coarse = w.CE.coarse;

  /* state.nets and state.chans are the arrays this edits in place.
     onChange runs after every tick, so the caller can repaint its own summary
     and verdict. */
  function SourcePicker(state, onChange) {
    this.state = state;
    this.onChange = onChange || function () {};
    this.cache = null;
  }

  SourcePicker.prototype.summary = function () {
    var picked = this.state.nets.concat(this.state.chans);
    return !picked.length ? 'Anywhere'
      : picked.length <= 2 ? picked.join(', ')
      : picked.length + ' selected';
  };

  /* The closed control, ready to drop into an option row. */
  SourcePicker.prototype.html = function (disabled, why) {
    return '<span class="multi' + (disabled ? ' na' : '') + '" id="multi">' +
      '<button type="button" class="multibtn" id="multibtn"' +
        (disabled ? ' disabled title="' + esc(why || '') + '"' : '') + '>' +
        '<span id="multisum">' + esc(this.summary()) + '</span>' +
        '<svg viewBox="0 0 24 24" width="13" height="13" fill="none" ' +
          'stroke="currentColor" stroke-width="2.4" stroke-linecap="round" ' +
          'stroke-linejoin="round"><path d="M6 9l6 6 6-6"/></svg></button>' +
      '<span class="multimenu" id="multimenu">' +
        '<input class="multiq" id="multiq" placeholder="Search..." autocomplete="off">' +
        '<span class="multibody" id="multibody"></span></span></span>';
  };

  SourcePicker.prototype.paint = function () {
    var sum = document.getElementById('multisum');
    if (sum) sum.textContent = this.summary();
    var btn = document.getElementById('multibtn');
    if (btn) {
      btn.classList.toggle('set', !!(this.state.nets.length || this.state.chans.length));
    }
  };

  /* Open upward when the panel has no room below. The menu sits in a scrolling
     box, so a menu that overflows is clipped rather than scrolled to. Measured
     a frame later, or it reads the empty box and never flips. */
  SourcePicker.prototype.place = function (btn, menu) {
    requestAnimationFrame(function () {
      menu.classList.remove('up');
      var panel = menu.closest('.ovlbox') || document.documentElement;
      var room = panel.getBoundingClientRect().bottom - btn.getBoundingClientRect().bottom;
      if (room < menu.offsetHeight + 16) menu.classList.add('up');
    });
  };

  SourcePicker.prototype.wire = function () {
    var self = this;
    var btn = document.getElementById('multibtn'),
        menu = document.getElementById('multimenu'),
        mq = document.getElementById('multiq');
    if (!btn || btn.disabled) return;

    btn.addEventListener('click', function (e) {
      e.preventDefault();
      if (!menu.classList.toggle('open')) return;
      self.load().then(function () {
        self.render();
        self.place(btn, menu);
        if (!coarse()) mq.focus();
      });
    });
    mq.addEventListener('input', function () {
      self.render();
      self.place(btn, menu);
    });
    // A label wraps the row, so a click inside would re-fire the button it is
    // labelled by and close the menu on every tick.
    menu.addEventListener('click', function (e) { e.stopPropagation(); });
    document.addEventListener('click', function (e) {
      var m = document.getElementById('multi');
      if (m && !m.contains(e.target)) menu.classList.remove('open');
    });
  };

  SourcePicker.prototype.load = function () {
    var self = this;
    if (this.cache) return Promise.resolve(this.cache);
    return fetch('/api/sources').then(function (r) { return r.json(); })
      .then(function (d) { self.cache = d; return d; });
  };

  SourcePicker.prototype.render = function () {
    var body = document.getElementById('multibody');
    if (!body || !this.cache) return;
    var st = this.state;
    var q = ((document.getElementById('multiq') || {}).value || '').toLowerCase();
    var h = '';

    function item(kind, value, label, note, on) {
      return '<label class="multirow"><input type="checkbox" data-' + kind + '="' +
        esc(value) + '"' + (on ? ' checked' : '') + '><span class="mt">' + esc(label) +
        '</span>' + (note ? '<span class="mn">' + esc(note) + '</span>' : '') + '</label>';
    }

    var nets = this.cache.networks.filter(function (n) {
      return !q || n.name.toLowerCase().indexOf(q) !== -1;
    });
    if (nets.length) {
      h += '<span class="multisec">Networks</span>';
      nets.forEach(function (n) {
        h += item('net', n.name, n.name, n.channels.join(', '),
                  st.nets.indexOf(n.name) !== -1);
      });
    }
    var chans = this.cache.channels.filter(function (c) {
      return !q || (c.vcn + ' ' + c.call_sign + ' ' + c.network)
        .toLowerCase().indexOf(q) !== -1;
    });
    if (chans.length) {
      h += '<span class="multisec">Channels</span>';
      chans.forEach(function (c) {
        h += item('ch', c.vcn, c.vcn + ' ' + c.call_sign, c.network,
                  st.chans.indexOf(c.vcn) !== -1);
      });
    }
    body.innerHTML = h || '<span class="multinone">Nothing matches.</span>';

    var self = this;
    function flip(list, v) {
      var i = list.indexOf(v);
      if (i === -1) list.push(v); else list.splice(i, 1);
      self.paint();
      self.onChange();
    }
    body.querySelectorAll('[data-net]').forEach(function (cb) {
      cb.addEventListener('change', function () { flip(st.nets, cb.dataset.net); });
    });
    body.querySelectorAll('[data-ch]').forEach(function (cb) {
      cb.addEventListener('change', function () { flip(st.chans, cb.dataset.ch); });
    });
  };

  w.CE.SourcePicker = SourcePicker;
})(window);
