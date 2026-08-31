// Shared page behaviour: theme switch, header, panels, sync button.
// Loaded by base.html after ce.js, sources.js and smartfilter.js.
// Theme switch. The document already carries the right theme from the head
// script; this only handles the click and remembers the answer.
(function () {
  var btn = document.getElementById('themebtn');
  if (!btn) return;
  var root = document.documentElement, meta = document.getElementById('themecolor');
  // Keep the browser chrome in step with the page. Read the value from the
  // stylesheet rather than repeating the hex here, so there is one source.
  function paintChrome() {
    if (meta) meta.setAttribute('content',
      getComputedStyle(root).getPropertyValue('--bg').trim() || '#040c15');
  }
  var txt = document.getElementById('themelabel');
  function label() {
    var to = root.getAttribute('data-theme') === 'light' ? 'dark' : 'light';
    btn.title = 'Switch to ' + to + ' theme';
    btn.setAttribute('aria-label', btn.title);
    if (txt) txt.textContent = to === 'light' ? 'Light mode' : 'Dark mode';
  }
  paintChrome(); label();
  btn.addEventListener('click', function () {
    var next = root.getAttribute('data-theme') === 'light' ? 'dark' : 'light';
    root.setAttribute('data-theme', next);
    try { localStorage.setItem('ce-theme', next); } catch (e) {}
    paintChrome(); label();
    // Signed in, the choice belongs to the account, so it follows the person
    // to another browser. Signed out there is nobody to attach it to and the
    // local copy above is the whole story.
    try {
      fetch('/api/theme', {method: 'POST', body: new URLSearchParams({theme: next})});
    } catch (e) {}
  });
  // Follow the system only while the user has expressed no preference.
  try {
    window.matchMedia('(prefers-color-scheme: light)').addEventListener('change', function (e) {
      if (root.getAttribute('data-theme-from') === 'account') return;
      if (localStorage.getItem('ce-theme')) return;
      root.setAttribute('data-theme', e.matches ? 'light' : 'dark');
      paintChrome(); label();
    });
  } catch (e) {}
})();

// Tab bar. Ludodex slides the underline and bursts sparks on click, inside a
// single-page app. These pages are server rendered, so the click ends in a
// page load and the slide would never be seen. Instead the bar remembers the
// tab you came from: on load it puts the line back there, then moves it to the
// tab you are on. The slide plays after the page is up, so it costs nothing.
// Adapted from CodeFronts "Particle Burst" CSS tabs (MIT), see THIRD_PARTY.md.
(function () {
  var nav = document.getElementById('ptnav'), line = document.getElementById('ptline');
  if (!nav || !line) return;
  var tabs = nav.querySelectorAll('.pt-tab');
  var here = nav.dataset.tab || '';
  var active = nav.querySelector('.pt-tab.active') || tabs[0];
  if (!active) return;

  function sit(el, animate) {
    line.classList.toggle('move', !!animate);
    line.style.left = el.offsetLeft + 'px';
    line.style.width = el.offsetWidth + 'px';
  }

  // The spark palette follows the theme, so the burst stays visible on paper
  // as well as on the dark ground.
  function colours() {
    var cs = getComputedStyle(document.documentElement);
    return ['--accent', '--live-dot', '--ok', '--info-fg', '--bad']
      .map(function (n) { return cs.getPropertyValue(n).trim() || '#f3a30b'; });
  }

  function burst(el) {
    if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) return;
    var r = el.getBoundingClientRect(), nr = nav.getBoundingClientRect();
    var cx = r.left - nr.left + nav.scrollLeft + r.width / 2;
    var cy = r.top - nr.top + r.height / 2;
    var pal = colours();
    for (var i = 0; i < 8; i++) {
      var sp = document.createElement('span');
      sp.className = 'pt-spark';
      sp.style.left = cx + 'px';
      sp.style.top = cy + 'px';
      sp.style.background = pal[i % pal.length];
      var angle = (i / 8) * Math.PI * 2, dist = 32 + Math.random() * 18;
      sp.style.setProperty('--dx', Math.cos(angle) * dist + 'px');
      sp.style.setProperty('--dy', Math.sin(angle) * dist + 'px');
      nav.appendChild(sp);
      (function (el2) { setTimeout(function () { el2.remove(); }, 700); })(sp);
    }
  }

  var from = null;
  try { from = sessionStorage.getItem('ce-tab'); } catch (e) {}
  var prev = from && from !== here
    ? nav.querySelector('.pt-tab[data-t="' + from + '"]') : null;

  // Start under the tab that was left, with no transition, then move.
  sit(prev || active, false);
  if (prev) {
    requestAnimationFrame(function () {
      requestAnimationFrame(function () { sit(active, true); burst(active); });
    });
  }
  try { sessionStorage.setItem('ce-tab', here); } catch (e) {}

  // A tab already under the pointer still bursts, so a click always answers.
  tabs.forEach(function (t) {
    t.addEventListener('click', function () {
      if (t === active) burst(t);
    });
  });

  window.addEventListener('resize', function () { sit(active, false); });
})();

// The programme panel. It lives here rather than on the guide, because the
// schedule and a pass both open the same thing.
// ---- programme overlay ----
// One place to see a programme and decide what to record. It lists every
// airing, not just the one clicked, because choosing the live broadcast over a
// rebroadcast is the entire point of this app.
(function () {
  var ovl = document.getElementById('ovl'), box = document.getElementById('ovlbox');
  if (!ovl || !box) return;

  var esc = CE.esc, when = CE.fmtWhen, coarse = CE.coarse;
  function close() { ovl.classList.remove('show'); box.innerHTML = ''; }

  ovl.addEventListener('click', function (e) { if (e.target === ovl) close(); });
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape' && ovl.classList.contains('show')) close();
  });

  function pickBest(airings) {
    var usable = airings.filter(function (a) { return !a.drm; });
    if (!usable.length) return null;
    var live = usable.filter(function (a) { return a.premiere; });
    var pool = live.length ? live : usable;
    return pool.reduce(function (m, a) { return a.b < m.b ? a : m; });
  }

  function render(d) {
    var best = pickBest(d.airings);
    var h = '';
    if (d.dry_run) {
      h += '<div class="banner warn ovlnote">Preview mode is on, so nothing is ' +
           'sent to Plex. Turn it off in <a href="/settings"><b>Settings</b></a> ' +
           'to record.</div>';
    }
    h += '<div class="ovlhead">';
    if (d.thumb) h += '<img class="ovlthumb" src="' + esc(d.thumb) + '" alt="">';
    h += '<div class="ovlmeta">';
    if (d.parent && d.parent !== d.title) h += '<div class="parent">' + esc(d.parent) + '</div>';
    h += '<h2>' + esc(d.title) + '</h2>';
    var bits = [];
    if (d.year) bits.push(d.year);
    (d.genres || []).slice(0, 3).forEach(function (g) { bits.push(esc(g)); });
    if (bits.length) h += '<div class="parent">' + bits.join(' &middot; ') + '</div>';
    if (d.summary) h += '<div class="sum">' + esc(d.summary) + '</div>';
    h += '</div><button class="ovlclose" id="ovlx" aria-label="Close">&times;</button></div>';

    // Why this is recording, before the list of airings, because it is the
    // first thing anyone opening a scheduled programme wants to know.
    if (d.why) {
      h += '<div class="optbar ' + (d.why.who === 'ce' ? 'ce' : 'plex') + '">' +
           '<i class="own ' + (d.why.who === 'ce' ? 'ce' : 'plex') + '"></i>' +
           '<span>' + esc(d.why.text) + '</span></div>';
    }

    h += '<div class="ovlsec"><h3>Airings</h3>';
    d.airings.forEach(function (a) {
      var isBest = best && a.id === best.id;
      h += '<div class="air' + (isBest ? ' best' : '') + '">';
      h += a.logo ? '<img src="/logo/' + esc(a.vcn) + '" alt="">' : '<span style="width:40px"></span>';
      h += '<div><div class="when">' + when(a.b) + '</div>' +
           '<div class="ch">' + esc(a.vcn) + ' ' + esc(a.call_sign) +
           (a.premiere ? ' &middot; <b>LIVE</b>' : '') + (a.drm ? ' &middot; DRM' : '') + '</div></div>';
      h += '<div class="act">';
      if (a.ours) {
        h += '<span class="pill ok">scheduled</span> ' +
             '<button class="danger" data-cancel="' + esc(a.id) + '">Cancel</button>';
      } else if (a.drm) h += '<span class="pill bad">cannot record</span>';
      else if (d.dry_run) {
        h += '<button disabled title="Preview mode is on">Record</button>';
      } else h += '<button data-rec="' + esc(a.id) + '">Record</button>';
      h += '</div></div>';
    });
    if (best && d.airings.length > 1) {
      h += '<div class="note" style="margin-top:10px">Highlighted is the live broadcast. ' +
           'The others are repeats of the same programme.</div>';
    }
    h += '</div>';

    if ((d.teams || []).length) {
      h += '<div class="ovlsec"><h3>Teams</h3><div class="row">';
      d.teams.forEach(function (t) {
        h += t.followed
          ? '<span class="pill ok">Following ' + esc(t.name) + '</span>'
          : '<button data-team="' + t.id + '">Follow ' + esc(t.name) + '</button>';
      });
      h += '</div><div class="note" style="margin-top:8px">A pass records every game this ' +
           'team plays, always from the live broadcast.</div></div>';
    }

    h += '<div class="ovlfoot"><span class="note" id="ovlmsg">';
    if (d.scheduled_by_us) h += 'CouchElephant scheduled this.';
    else if (d.scheduled) h += 'Plex already has this: <b>' + esc(d.scheduled) + '</b>.';
    h += '</span></div>';
    box.innerHTML = h;

    document.getElementById('ovlx').addEventListener('click', close);
    box.querySelectorAll('[data-rec]').forEach(function (b) {
      b.addEventListener('click', function () { openOptions(b.dataset.rec, d); });
    });
    box.querySelectorAll('[data-cancel]').forEach(function (b) {
      b.addEventListener('click', function () {
        post('/api/record/cancel', {airing_id: b.dataset.cancel}, b, b.dataset.cancel);
      });
    });
    box.querySelectorAll('[data-team]').forEach(function (b) {
      b.addEventListener('click', function () { post('/api/pass', {team_id: b.dataset.team}, b); });
    });
  }

  function say(text, kind) {
    var msg = document.getElementById('ovlmsg');
    if (!msg) return;
    msg.className = 'note said' + (kind ? ' ' + kind : '');
    msg.textContent = text;
    // The panel scrolls, and the reply sits at its foot. Bring it into view or
    // a failure looks the same as no response at all.
    if (msg.scrollIntoView) msg.scrollIntoView({block: 'nearest', behavior: 'smooth'});
  }

  // ---- the options step ----
  // Plex offers more than "record this". It can also follow the series or the
  // team, and each choice carries its own settings. Those come from Plex's own
  // template, so this panel shows whatever Plex offers rather than a list
  // copied here that would go stale.
  var optState = null;

  // One row per option. The little rectangle says whose feature it is, so the
  // two systems read as one list rather than two lists the user has to join up.
  // The option row and the Plex setting renderer are shared. See
  // static/js/ce.js: they existed twice and had already drifted.
  var row = CE.optRow, field = CE.settingField;


  // The source limit is the shared picker, not a copy. See static/js/sources.js.
  function multiField() {
    var t = optState.templates[optState.pick];
    var c = optState.picker.html(
      t.one_shot,
      'A single broadcast is already one channel at one time, so there is ' +
      'nothing to narrow.');
    return row('ce', 'multibtn', 'Limit to networks or channels', c,
               t.one_shot ? '' : 'Plex takes one channel. This takes as many as you like.',
               'wide');
  }

  // Which side owns the recording. Plex settings map onto one Plex
  // subscription; CouchElephant settings do not, so CouchElephant has to keep
  // the rule and pin each airing itself. Saying which is about to happen is
  // the point of the bar at the top.
  function verdict() {
    var t = optState.templates[optState.pick];
    return (optState.nets.length || optState.chans.length) && !t.one_shot ? 'ce' : 'plex';
  }

  // Only the parts that change are redrawn. Rebuilding the panel on every
  // checkbox would close the dropdown under the user's finger.
  function paintOptions() {
    optState.picker.paint();
    var v = verdict(), bar = document.getElementById('optbar');
    if (bar) {
      bar.className = 'optbar ' + v;
      bar.innerHTML = '<i class="own ' + v + '"></i>' + (v === 'ce'
        ? '<span><b>CouchElephant schedule.</b> It watches the guide and books ' +
          'each airing itself, because a Plex rule takes one channel and you ' +
          'have named more than one source.</span>'
        : '<span><b>Plex schedule.</b> This becomes an ordinary Plex recording, ' +
          'and Plex takes it from here.</span>');
    }
  }

  function renderOptions(o, prog, airingId) {
    var t = optState.templates[optState.pick];
    var h = '';
    h += '<div class="ovlhead"><div class="ovlmeta">';
    h += '<div class="parent">Record</div><h2>' + esc(o.title) + '</h2>';
    h += '</div><button class="ovlclose" id="ovlx" aria-label="Close">&times;</button></div>';

    h += '<div class="optbar" id="optbar"></div>';

    h += '<div class="ovlsec"><h3>What to record</h3>';
    optState.templates.forEach(function (x, i) {
      h += '<label class="pickrow' + (i === optState.pick ? ' on' : '') + '">' +
           '<input type="radio" name="tpl" value="' + i + '"' +
           (i === optState.pick ? ' checked' : '') + '>' +
           '<span>' + esc(x.title) + '</span>' +
           '<span class="note">' + (x.one_shot ? 'this broadcast only'
             : 'keeps matching new airings') + '</span></label>';
    });
    h += '</div>';

    h += '<div class="ovlsec"><h3>Options</h3>';
    h += '<div class="ownkey"><span><i class="own ce"></i>CouchElephant</span>' +
         '<span><i class="own plex"></i>Plex DVR</span></div>';
    if (t.one_shot) {
      h += '<div class="note" style="margin-bottom:10px">CouchElephant has already set ' +
           '<b>Limit to channel</b> and <b>Limit to airing time</b> to the broadcast ' +
           'you picked. That is what stops Plex choosing a repeat.</div>';
    }
    h += '<div class="optgrid">';
    h += multiField();
    t.settings.forEach(function (st) { h += field(st); });
    h += '</div></div>';

    h += '<div class="ovlfoot">' +
         '<button class="primary" id="optgo">Schedule</button>' +
         '<button id="optback">Back</button>' +
         '<span class="note" id="ovlmsg"></span></div>';
    box.innerHTML = h;

    document.getElementById('ovlx').addEventListener('click', close);
    document.getElementById('optback').addEventListener('click', function () {
      window.openProgram(prog.airing_id || airingId);
    });
    box.querySelectorAll('input[name=tpl]').forEach(function (r) {
      r.addEventListener('change', function () {
        optState.pick = +r.value;
        renderOptions(o, prog, airingId);
      });
    });

    optState.picker.wire();

    document.getElementById('optgo').addEventListener('click', function (e) {
      var vals = {};
      box.querySelectorAll('[data-set]').forEach(function (el) {
        vals[el.dataset.set] = el.dataset.bool ? (el.checked ? 'true' : 'false') : el.value;
      });
      post('/api/record', {
        airing_id: airingId, template: optState.pick,
        settings: JSON.stringify(vals),
        networks: JSON.stringify(optState.nets),
        channels: JSON.stringify(optState.chans)
      }, e.target, airingId);
    });
    paintOptions();
  }

  function openOptions(airingId, prog) {
    box.innerHTML = '<div class="empty">Asking Plex what it can record...</div>';
    fetch('/api/record/options?airing_id=' + encodeURIComponent(airingId))
      .then(function (r) { return r.json(); })
      .then(function (o) {
        if (!o.ok) { box.innerHTML = '<div class="empty">' + esc(o.error) + '</div>'; return; }
        optState = {templates: o.templates, pick: 0, opts: o, nets: [], chans: []};
        optState.picker = new CE.SourcePicker(optState, paintOptions);
        renderOptions(o, prog, airingId);
      })
      .catch(function (e) { box.innerHTML = '<div class="empty">' + esc(e) + '</div>'; });
  }

  function post(url, data, btn, reopen) {
    btn.disabled = true;
    var old = btn.textContent;
    btn.textContent = 'Working...';
    say('Working...', '');
    var body = new URLSearchParams(data);
    fetch(url, {method: 'POST', body: body})
      .then(function (r) {
        return r.text().then(function (t) {
          var j;
          try { j = JSON.parse(t); }
          // A proxy error page or a traceback is not JSON. Show the status
          // instead of dying in the parser, where nothing reaches the user.
          catch (e) { j = {ok: false, error: 'HTTP ' + r.status + ': ' + t.slice(0, 200)}; }
          return {ok: r.ok, j: j};
        });
      })
      .then(function (res) {
        if (res.j.ok) {
          say(res.j.message || 'Done.', 'ok');
          btn.textContent = 'Done';
          // Show the programme again so the airing row reflects what just
          // happened: a scheduled airing offers Cancel, a cancelled one Record.
          // Keep the grid behind the overlay honest too, rather than leaving a
          // block that still looks unscheduled until the next page load.
          var blk = document.querySelector('.gprog[data-aid="' +
            String(reopen).replace(/"/g, '\\"') + '"]');
          if (blk) {
            var on = url.indexOf('cancel') === -1;
            blk.classList.toggle('sched-ce', on);
            var t = blk.querySelector('.t');
            if (t && on && !t.querySelector('.rec')) {
              t.insertAdjacentHTML('afterbegin', '<i class="rec"></i>');
            }
            if (t && !on) { var i = t.querySelector('.rec'); if (i) i.remove(); }
          }
          if (reopen) setTimeout(function () { window.openProgram(reopen); }, 650);
        } else {
          say(res.j.error || res.j.message || ('Request failed (HTTP ' + res.ok + ').'), 'bad');
          btn.disabled = false; btn.textContent = old;
        }
      })
      .catch(function (e) {
        say('Could not reach CouchElephant: ' + e, 'bad');
        btn.disabled = false; btn.textContent = old;
      });
  }

  window.openProgram = function (aid) {
    box.innerHTML = '<div class="empty">Loading...</div>';
    ovl.classList.add('show');
    fetch('/api/program?airing_id=' + encodeURIComponent(aid))
      .then(function (r) { return r.json(); })
      .then(function (d) { d.error ? box.innerHTML = '<div class="empty">' + esc(d.error) + '</div>' : render(d); })
      .catch(function (e) { box.innerHTML = '<div class="empty">' + esc(e) + '</div>'; });
  };
})();

// The settings window. One file renders it, so the gear can show it over the
// page you are on while /settings still serves the same thing whole.
window.wireSettings = function (root) {
  var nav = root.querySelector('#setnav'), tabs = root.querySelector('#settabs'),
      content = root.querySelector('#setcontent'), q = root.querySelector('#setq'),
      none = root.querySelector('#setnone');
  if (!nav || !tabs || !content) return;
  var secs = Array.prototype.slice.call(content.querySelectorAll('section'));

  function show(sec, tab) {
    nav.querySelectorAll('.nav-item').forEach(function (b) {
      b.classList.toggle('sel', b.dataset.sec === sec);
    });
    var mine = secs.filter(function (s) { return s.dataset.sec === sec; });
    if (!mine.length) return;
    tab = tab || mine[0].dataset.tab;
    // A section with one tab shows no tab strip. A single tab is not a choice.
    tabs.innerHTML = mine.length > 1 ? mine.map(function (s) {
      return '<button type="button" class="tab' + (s.dataset.tab === tab ? ' sel' : '') +
             '" data-tab="' + s.dataset.tab + '">' + s.dataset.tabLabel + '</button>';
    }).join('') : '';
    tabs.querySelectorAll('.tab').forEach(function (b) {
      b.addEventListener('click', function () { show(sec, b.dataset.tab); });
    });
    secs.forEach(function (s) {
      s.hidden = !(s.dataset.sec === sec && s.dataset.tab === tab);
    });
    content.scrollTop = 0;
  }

  nav.querySelectorAll('.nav-item').forEach(function (b) {
    b.addEventListener('click', function () { show(b.dataset.sec); });
  });

  // Search across every section, not just the one on screen, and say where
  // each match lives so the answer is one click away.
  function search() {
    var term = (q.value || '').trim().toLowerCase();
    root.querySelectorAll('.sethit').forEach(function (e) { e.classList.remove('sethit'); });
    if (!term) {
      nav.querySelectorAll('.nav-item').forEach(function (b) {
        b.hidden = false;
        var h = b.querySelector('.hits'); if (h) h.remove();
      });
      if (none) none.hidden = true;
      return;
    }
    var any = false;
    nav.querySelectorAll('.nav-item').forEach(function (b) {
      var hits = 0;
      secs.filter(function (s) { return s.dataset.sec === b.dataset.sec; })
        .forEach(function (s) {
          s.querySelectorAll('[data-find]').forEach(function (f) {
            var hay = (f.dataset.find + ' ' + f.textContent).toLowerCase();
            if (hay.indexOf(term) !== -1) { hits++; f.classList.add('sethit'); }
          });
        });
      b.hidden = hits === 0;
      if (hits) any = true;
      var h = b.querySelector('.hits');
      if (hits) {
        if (!h) { h = document.createElement('span'); h.className = 'hits'; b.appendChild(h); }
        h.textContent = hits;
      } else if (h) h.remove();
    });
    if (none) none.hidden = any;
    // Jump to the first section that matched, so a search answers itself.
    var first = nav.querySelector('.nav-item:not([hidden])');
    if (first) show(first.dataset.sec);
  }
  if (q) q.addEventListener('input', search);

  // Saving stays in the window. The endpoints redirect back to the settings
  // page, which a fetch follows and reads as success.
  // A check reports a verdict rather than a saved marker: a tick or a cross,
  // and the sentence that says what to do about it.
  function runCheck(f) {
    var out = f.parentElement.querySelector('.verdict');
    var btn = f.querySelector('button'), old = btn ? btn.textContent : '';
    if (out) {
      out.hidden = false;
      out.className = 'verdict busy';
      out.querySelector('.vtext').textContent = 'Checking...';
    }
    if (btn) { btn.disabled = true; btn.textContent = 'Checking...'; }
    fetch(f.action, {method: 'POST', headers: {Accept: 'application/json'},
                     body: new URLSearchParams(new FormData(f))})
      .then(function (r) { return r.json(); })
      .then(function (j) {
        if (out) {
          out.className = 'verdict ' + (j.ok ? 'ok' : 'bad');
          out.querySelector('.vtext').textContent = j.detail || (j.ok ? 'Fine.' : 'Failed.');
        }
      })
      .catch(function (err) {
        if (out) {
          out.className = 'verdict bad';
          out.querySelector('.vtext').textContent =
            'Could not reach CouchElephant itself: ' + err;
        }
      })
      .then(function () { if (btn) { btn.disabled = false; btn.textContent = old; } });
  }

  root.querySelectorAll('form.setform').forEach(function (f) {
    f.addEventListener('submit', function (e) {
      e.preventDefault();
      if (f.dataset.verdict) { runCheck(f); return; }
      var btn = f.querySelector('button'), tell = f.querySelector('.saved') ||
        (f.parentElement && f.parentElement.querySelector('.saved'));
      var old = btn ? btn.textContent : '';
      if (btn) { btn.disabled = true; btn.textContent = 'Working...'; }
      fetch(f.action, {method: 'POST', body: new URLSearchParams(new FormData(f))})
        .then(function (r) {
          if (tell) {
            tell.className = 'saved on' + (r.ok ? '' : ' bad');
            tell.textContent = r.ok ? 'Saved' : 'Failed';
            setTimeout(function () { tell.className = 'saved'; }, 2400);
          }
          if (btn) { btn.disabled = false; btn.textContent = old; }
          // Removing a user changes what the window lists, so that one reloads
          // rather than leaving a row that no longer exists on screen.
          if (f.action.indexOf('/delete') !== -1) location.reload();
        })
        .catch(function () {
          if (tell) { tell.className = 'saved on bad'; tell.textContent = 'Failed'; }
          if (btn) { btn.disabled = false; btn.textContent = old; }
        });
    });
  });

  // ---- Backup and restore -------------------------------------------
  //
  // Three panels over one idea: get your decisions out of this machine and
  // back into it. Export is by hand, snapshots are on a timer, the backing
  // store is live. Each one has its own restore.

  function verdict(el, ok, text) {
    if (!el) return;
    el.hidden = false;
    el.className = 'verdict ' + (ok === null ? 'busy' : ok ? 'ok' : 'bad');
    var t = el.querySelector('.vtext');
    if (t) t.textContent = text;
  }

  var expgo = root.querySelector('#expgo');
  if (expgo) {
    expgo.addEventListener('click', function () {
      var secrets = root.querySelector('#expsecrets');
      // A plain navigation, so the browser saves it the way it saves anything.
      location.href = '/api/export' + (secrets && secrets.checked ? '?secrets=1' : '');
    });
  }

  var impfile = root.querySelector('#impfile');
  if (impfile) {
    var impgo = root.querySelector('#impgo');
    impfile.addEventListener('change', function () {
      var f = impfile.files[0];
      impgo.disabled = true;
      if (!f) return;
      var body = new FormData();
      body.append('file', f);
      verdict(root.querySelector('#impverdict'), null, 'Reading the file...');
      fetch('/api/import/inspect', {method: 'POST', body: body})
        .then(CE.readJson).then(function (d) {
          var v = root.querySelector('#impverdict');
          if (!d.ok) { verdict(v, false, d.error); return; }
          var bits = [];
          Object.keys(d.counts || {}).forEach(function (k) {
            if (d.counts[k]) bits.push(d.counts[k] + ' ' + k.replace(/_/g, ' '));
          });
          verdict(v, true, 'A CouchElephant export' +
            (d.created_at ? ' from ' + new Date(d.created_at * 1000).toLocaleString() : '') +
            (d.includes_secrets ? ', with the Plex token' : '') + '.');
          var what = root.querySelector('#impwhat');
          what.hidden = false;
          what.textContent = bits.length ? 'It holds ' + bits.join(', ') + '.'
                                         : 'It holds nothing.';
          impgo.disabled = false;
        })
        .catch(function (e) { verdict(root.querySelector('#impverdict'), false, String(e)); });
    });

    impgo.addEventListener('click', function () {
      var f = impfile.files[0], msg = root.querySelector('#impmsg');
      if (!f) return;
      var body = new FormData();
      body.append('file', f);
      if (root.querySelector('#impreplace').checked) body.append('replace', '1');
      body.append('secrets', '1');
      impgo.disabled = true; impgo.textContent = 'Importing...';
      fetch('/api/import', {method: 'POST', body: body})
        .then(CE.readJson).then(function (d) {
          msg.className = 'note said ' + (d.ok ? 'ok' : 'bad');
          msg.textContent = d.message || d.error || '';
          impgo.textContent = 'Import';
          impgo.disabled = !d.ok ? false : true;
          if (d.ok) setTimeout(function () { location.reload(); }, 1400);
        })
        .catch(function (e) {
          msg.className = 'note said bad'; msg.textContent = String(e);
          impgo.disabled = false; impgo.textContent = 'Import';
        });
    });
  }

  // ---- snapshot jobs ----
  var bkjobs = root.querySelector('#bkjobs');
  if (bkjobs) {
    function jobRow(j) {
      var id = j ? j.id : '';
      var h = '<div class="setblock bkjob" data-id="' + id + '">';
      h += '<div class="field"><label>Name</label>' +
           '<input data-f="name" value="' + CE.esc(j ? j.name : 'Nightly') + '"></div>';
      h += '<div class="field"><label>Folder</label>' +
           '<input data-f="dest_path" placeholder="/data/backups" value="' +
           CE.esc(j ? (j.dest_path || '') : '') + '">' +
           '<span class="help">A path this container can see. Mount it if it is ' +
           'somewhere else.</span></div>';
      h += '<div class="field"><label>Every</label>' +
           '<input type="number" min="0" data-f="every_hours" style="max-width:120px" ' +
           'value="' + (j ? j.every_hours : 24) + '">' +
           '<span class="help">Hours. 0 runs only when you press the button.</span></div>';
      h += '<div class="field"><label>Keep</label>' +
           '<input type="number" min="0" data-f="retention" style="max-width:120px" ' +
           'value="' + (j ? j.retention : 7) + '">' +
           '<span class="help">Archives. The oldest go first, and only this ' +
           'job\'s.</span></div>';
      h += '<div class="field"><label>Passphrase</label>' +
           '<input type="password" data-f="passphrase" placeholder="' +
           (j && j.encrypted ? 'set, leave blank to keep' : 'none') + '">' +
           '<span class="help">Optional. AES-256, which 7-Zip, Keka and WinRAR ' +
           'can open. Type <b>off</b> to remove one.</span></div>';
      h += '<label class="chk"><input type="checkbox" data-f="enabled"' +
           (!j || j.enabled ? ' checked' : '') + '><span>On</span></label>';
      h += '<label class="chk"><input type="checkbox" data-f="raw_db"' +
           (!j || j.raw_db ? ' checked' : '') +
           '><span>Include the database files as well as the export</span></label>';
      h += '<label class="chk"><input type="checkbox" data-f="with_secrets"' +
           (j && j.with_secrets ? ' checked' : '') +
           '><span>Include the Plex token</span></label>';
      if (j && j.last_run) {
        h += '<div class="note ' + (j.last_ok ? '' : 'said bad') + '">Last run ' +
             new Date(j.last_run * 1000).toLocaleString() + ': ' +
             (j.last_ok ? CE.esc(j.last_file || '') + ' (' +
               Math.round((j.last_size || 0) / 1024) + ' KB)'
                        : CE.esc(j.last_error || 'failed')) + '</div>';
      }
      h += '<div class="row" style="margin-top:10px">' +
           '<button type="button" class="primary" data-act="save">Save</button>' +
           (j ? '<button type="button" data-act="run">Back up now</button>' +
                '<button type="button" class="danger" data-act="del">Remove</button>' : '') +
           '<span class="note bksay"></span></div>';
      return h + '</div>';
    }

    function paintJobs(jobs) {
      bkjobs.innerHTML = jobs.length
        ? jobs.map(function (j) { return jobRow(j); }).join('')
        : '<div class="empty tiny">No backup jobs yet.</div>';
    }

    function loadJobs() {
      fetch('/api/backups/jobs').then(CE.readJson).then(function (d) {
        if (d.ok) paintJobs(d.jobs);
      });
    }

    root.querySelector('#bkadd').addEventListener('click', function () {
      bkjobs.insertAdjacentHTML('beforeend', jobRow(null));
    });

    bkjobs.addEventListener('click', function (e) {
      var btn = e.target.closest('[data-act]');
      if (!btn) return;
      var card = btn.closest('.bkjob'), id = card.dataset.id;
      function after(d) {
        // Repaint FIRST. Writing the message and then redrawing the list threw
        // the message away in the same tick, which read as "nothing happened".
        var newId = d.id || id;
        if (d.jobs) paintJobs(d.jobs);
        var row = bkjobs.querySelector('.bkjob[data-id="' + newId + '"]') || card;
        var say = row.querySelector('.bksay');
        if (!say) return;
        say.className = 'note bksay said ' + (d.ok ? 'ok' : 'bad');
        say.textContent = d.ok ? (d.file ? 'Wrote ' + d.file : 'Saved')
                               : (d.error || 'Failed');
      }
      if (btn.dataset.act === 'save') {
        var body = new URLSearchParams();
        if (id) body.append('job_id', id);
        card.querySelectorAll('[data-f]').forEach(function (el) {
          body.append(el.dataset.f, el.type === 'checkbox' ? (el.checked ? '1' : '') : el.value);
        });
        fetch('/api/backups/jobs', {method: 'POST', body: body}).then(CE.readJson).then(after);
      } else if (btn.dataset.act === 'run') {
        btn.disabled = true; btn.textContent = 'Working...';
        fetch('/api/backups/jobs/' + id + '/run', {method: 'POST'})
          .then(CE.readJson).then(function (d) {
            btn.disabled = false; btn.textContent = 'Back up now'; after(d);
          });
      } else if (btn.dataset.act === 'del') {
        fetch('/api/backups/jobs/' + id + '/delete', {method: 'POST'})
          .then(CE.readJson).then(after);
      }
    });

    root.querySelector('#bklist').addEventListener('click', function () {
      var dest = root.querySelector('#bkdest').value.trim();
      var out = root.querySelector('#bkarch');
      out.innerHTML = '<div class="fempty">Reading...</div>';
      fetch('/api/backups/archives?dest=' + encodeURIComponent(dest))
        .then(CE.readJson).then(function (d) {
          if (!d.archives.length) {
            out.innerHTML = '<div class="fempty">Nothing in that folder.</div>';
            return;
          }
          out.innerHTML = d.archives.map(function (a) {
            return '<button type="button" class="rrow" data-arch="' + CE.esc(a.name) +
              '"><span class="rt">' + CE.esc(a.name) + '</span><span class="rn">' +
              new Date(a.at * 1000).toLocaleString() + ' &middot; ' +
              Math.round(a.size / 1024) + ' KB</span></button>';
          }).join('');
          out.querySelectorAll('[data-arch]').forEach(function (b) {
            b.addEventListener('click', function () {
              var name = b.dataset.arch;
              // Restoring replaces what is here. It is worth one question.
              if (!confirm('Restore ' + name + '? A copy of what is here now is ' +
                           'kept first.')) return;
              var pass = prompt('Passphrase, if that archive has one:', '') || '';
              var body = new URLSearchParams({dest: dest, name: name,
                                              passphrase: pass, replace: '1'});
              b.disabled = true;
              fetch('/api/backups/restore', {method: 'POST', body: body})
                .then(CE.readJson).then(function (d2) {
                  b.disabled = false;
                  if (d2.ok) { location.reload(); return; }
                  alert(d2.error || 'That did not work.');
                });
            });
          });
        });
    });

    loadJobs();
  }

  // ---- the backing store ----
  var bsbackend = root.querySelector('#bsbackend');
  if (bsbackend) {
    var META = null;

    function paintFields() {
      var wrap = root.querySelector('#bsfields');
      var chosen = bsbackend.value;
      var b = (META.backends || []).filter(function (x) { return x.name === chosen; })[0];
      if (!b) { wrap.innerHTML = ''; return; }
      wrap.innerHTML = b.fields.map(function (f) {
        return '<div class="field"><label for="bs_' + f.key + '">' + CE.esc(f.label) +
          '</label><input id="bs_' + f.key + '" data-k="' + f.key + '" type="' +
          (f.kind === 'secret' ? 'password' : 'text') + '" value="' +
          CE.esc(META.config[f.key] || '') + '"></div>';
      }).join('');
    }

    function paintStatus() {
      var el = root.querySelector('#bsstatus'), st = META.status || {};
      if (!st.at) { el.textContent = 'It has not run yet.'; return; }
      el.className = 'note said ' + (st.ok ? 'ok' : 'bad');
      el.textContent = new Date(st.at * 1000).toLocaleString() + ': ' + (st.detail || '');
    }

    fetch('/api/backingstore/config').then(CE.readJson).then(function (d) {
      if (!d.ok) return;
      META = d;
      bsbackend.innerHTML = '<option value="">Not set up</option>' +
        d.backends.map(function (b) {
          return '<option value="' + b.name + '"' +
            (d.config.backingstore_backend === b.name ? ' selected' : '') + '>' +
            CE.esc(b.label) + '</option>';
        }).join('');
      root.querySelector('#bsauto').value = d.config.backingstore_auto_minutes || 0;
      paintFields();
      paintStatus();
    });

    bsbackend.addEventListener('change', paintFields);

    function saveStore() {
      var body = new URLSearchParams();
      body.append('backingstore_backend', bsbackend.value);
      body.append('backingstore_auto_minutes', root.querySelector('#bsauto').value || '0');
      root.querySelectorAll('#bsfields [data-k]').forEach(function (el) {
        body.append(el.dataset.k, el.value);
      });
      return fetch('/api/backingstore/config', {method: 'POST', body: body})
        .then(CE.readJson);
    }

    root.querySelector('#bssave').addEventListener('click', function () {
      saveStore().then(function () {
        verdict(root.querySelector('#bsverdict'), true, 'Saved.');
      });
    });

    root.querySelector('#bstest').addEventListener('click', function () {
      var v = root.querySelector('#bsverdict');
      verdict(v, null, 'Checking...');
      // Saved first, so the test checks what is on screen rather than what
      // was last stored.
      saveStore().then(function () {
        return fetch('/api/backingstore/test', {method: 'POST'}).then(CE.readJson);
      }).then(function (d) { verdict(v, !!d.ok, d.detail || d.error || ''); })
        .catch(function (e) { verdict(v, false, String(e)); });
    });

    function run(url, body, label) {
      var el = root.querySelector('#bsstatus');
      el.className = 'note'; el.textContent = 'Working...';
      fetch(url, {method: 'POST', body: body}).then(CE.readJson).then(function (d) {
        el.className = 'note said ' + (d.ok ? 'ok' : 'bad');
        if (!d.ok) { el.textContent = d.error || 'That did not work.'; return; }
        el.textContent = d.dry_run
          ? label + ' would send ' + d.pushed + ', receive ' + d.pulled +
            ' and remove ' + d.removed + '.'
          : (d.restored != null
              ? 'Restored ' + d.restored + ' record(s).'
              : 'Sent ' + d.pushed + ', received ' + d.pulled +
                ', removed ' + d.removed + '.');
      });
    }

    root.querySelector('#bsrun').addEventListener('click', function () {
      run('/api/backingstore/run', new URLSearchParams(), 'That');
    });
    root.querySelector('#bsdry').addEventListener('click', function () {
      run('/api/backingstore/run', new URLSearchParams({dry_run: '1'}), 'That');
    });
    root.querySelector('#bsrestore').addEventListener('click', function () {
      if (!confirm('Pull everything down from the backing store? Nothing is ' +
                   'written back to it.')) return;
      run('/api/backingstore/restore', new URLSearchParams(), 'That');
    });
  }

  // Channel artwork: upload one of your own, or drop back to the guide's.
  var chlist = root.querySelector('#chlist');
  if (chlist) {
    var chq = root.querySelector('#chq');
    if (chq) chq.addEventListener('input', function () {
      var t = chq.value.trim().toLowerCase();
      chlist.querySelectorAll('.chrow').forEach(function (r) {
        r.hidden = !!t && (r.dataset.find || '').toLowerCase().indexOf(t) === -1;
      });
    });

    function tell(rowEl, text, kind) {
      var s2 = rowEl.querySelector('.chsay');
      s2.className = 'chsay' + (kind ? ' ' + kind : '');
      s2.textContent = text || '';
    }

    function afterChange(rowEl, j, custom) {
      // Bust the cache with the version the server just handed back, or the
      // browser keeps showing the logo that was replaced.
      var img = rowEl.querySelector('.chart');
      img.src = '/logo/' + encodeURIComponent(rowEl.dataset.vcn) + '?v=' + (j.v || Date.now());
      var pill = rowEl.querySelector('.chsrc');
      pill.textContent = custom ? 'yours' : 'guide';
      pill.classList.toggle('ce', custom);
      rowEl.querySelector('.chreset').hidden = !custom;
      rowEl.querySelector('.btnlike').textContent = 'Replace';
      tell(rowEl, j.message || '', 'ok');
    }

    chlist.querySelectorAll('.chrow').forEach(function (rowEl) {
      var file = rowEl.querySelector('input[type=file]');
      file.addEventListener('change', function () {
        if (!file.files || !file.files[0]) return;
        var fd = new FormData();
        fd.append('logo', file.files[0]);
        tell(rowEl, 'Uploading...', '');
        fetch('/settings/channels/' + encodeURIComponent(rowEl.dataset.vcn) + '/logo',
              {method: 'POST', body: fd})
          .then(function (r) { return r.json(); })
          .then(function (j) {
            if (j.ok) afterChange(rowEl, j, true);
            else tell(rowEl, j.error || 'That did not work.', 'bad');
          })
          .catch(function (err) { tell(rowEl, String(err), 'bad'); })
          .then(function () { file.value = ''; });
      });
      rowEl.querySelector('.chreset').addEventListener('click', function () {
        tell(rowEl, 'Resetting...', '');
        fetch('/settings/channels/' + encodeURIComponent(rowEl.dataset.vcn) + '/logo/reset',
              {method: 'POST'})
          .then(function (r) { return r.json(); })
          .then(function (j) {
            if (j.ok) afterChange(rowEl, j, false);
            else tell(rowEl, j.error || 'That did not work.', 'bad');
          })
          .catch(function (err) { tell(rowEl, String(err), 'bad'); });
      });
    });
  }

  show('plex');
};

(function () {
  var gear = document.getElementById('gearbtn');
  var ovl = document.getElementById('ovl'), box = document.getElementById('ovlbox');
  var here = document.getElementById('setwin');
  if (here) { window.wireSettings(here); return; }   // already the whole page
  if (!gear || !ovl || !box) return;
  gear.addEventListener('click', function (e) {
    // A plain click opens it here. Anything modified keeps the link honest.
    if (e.metaKey || e.ctrlKey || e.shiftKey || e.button !== 0) return;
    e.preventDefault();
    box.innerHTML = '<div class="empty">Loading...</div>';
    ovl.classList.add('show', 'settings');
    fetch('/partial/settings').then(function (r) { return r.text(); })
      .then(function (html) {
        box.innerHTML = html;
        window.wireSettings(box);
        var x = box.querySelector('#setclose');
        if (x) x.addEventListener('click', shut);
      })
      .catch(function (err) { box.innerHTML = '<div class="empty">' + err + '</div>'; });
  });
  function shut() { ovl.classList.remove('show', 'settings'); box.innerHTML = ''; }
  ovl.addEventListener('click', function (e) { if (e.target === ovl) shut(); });
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape' && ovl.classList.contains('settings')) shut();
  });
})();

// The account menu.
(function () {
  var wrap = document.getElementById('pwrap'), btn = document.getElementById('pbtn'),
      menu = document.getElementById('pmenu');
  if (!wrap || !btn || !menu) return;
  function shut() {
    menu.classList.remove('open');
    btn.classList.remove('open');
    btn.setAttribute('aria-expanded', 'false');
  }
  btn.addEventListener('click', function (e) {
    e.stopPropagation();
    var open = menu.classList.toggle('open');
    btn.classList.toggle('open', open);
    btn.setAttribute('aria-expanded', open ? 'true' : 'false');
  });
  // The theme button lives inside, and closing on its click would hide the
  // label that just changed.
  menu.addEventListener('click', function (e) { e.stopPropagation(); });
  document.addEventListener('click', shut);
  document.addEventListener('keydown', function (e) { if (e.key === 'Escape') shut(); });
})();

// Spin the sync icon while the request is in flight. A full page reload
// follows, so this only has to survive until the server answers.
//
// The icon that spins is always the one in the bar, which is not always the
// button that was pressed: with a problem open, the sync lives in the panel.
(function () {
  var f = document.querySelector('form[action="/sync"]');
  if (!f) return;
  f.addEventListener('submit', function () {
    var icon = document.getElementById('syncbtn');
    if (icon) icon.classList.add('spinning');
    var b = f.querySelector('button');
    if (!b) return;
    b.disabled = true;
    // A text button cannot spin, so it says so instead.
    if (b.dataset.busy) b.textContent = b.dataset.busy;
  });
})();

// The health notices, hung off the sync button.
//
// Two controls open this panel: the badge, and the sync icon under it. Once
// Plex has a problem the icon stops syncing and reads the problem instead,
// because syncing is the reflex and it only re-reads a guide that has not
// moved. Neither control is inside the sync form, so opening the panel cannot
// start a minute of work nobody asked for. The sync itself is in the panel.
(function () {
  var menu = document.getElementById('noticemenu');
  if (!menu) return;
  var triggers = [].slice.call(document.querySelectorAll('[data-notice-toggle]'));
  if (!triggers.length) return;
  function set(open) {
    menu.classList.toggle('open', open);
    for (var i = 0; i < triggers.length; i++) {
      triggers[i].setAttribute('aria-expanded', open ? 'true' : 'false');
    }
  }
  function shut() { set(false); }
  triggers.forEach(function (t) {
    t.addEventListener('click', function (e) {
      e.preventDefault();
      e.stopPropagation();
      set(!menu.classList.contains('open'));
    });
  });
  // The sync form lives inside. Closing on its click would take the panel away
  // before the click became a submit.
  menu.addEventListener('click', function (e) { e.stopPropagation(); });
  document.addEventListener('click', shut);
  document.addEventListener('keydown', function (e) { if (e.key === 'Escape') shut(); });
})();

// The plans: what a pass is waiting for that the guide has not reached yet.
//
// Fetched rather than rendered server side, because /recordings is a shell:
// the schedule and the rules on that page arrive the same way. The dates are
// already formatted by the server, because `precision` decides the format and
// a client left to guess would put a time on the screen nobody published.
(function () {
  var card = document.getElementById('plancard'),
      list = document.getElementById('planlist');
  if (!card || !list) return;
  fetch('/api/expectations')
    .then(function (r) { return r.json(); })
    .then(function (body) {
      var rows = (body && body.rows) || [];
      if (!rows.length) return;
      rows.forEach(function (e) {
        var el = document.createElement('div');
        el.className = 'plan';
        el.setAttribute('data-expectation', e.source_id);
        var parts = ['<span class="plan-title"></span>'];
        if (e.subtitle) parts.push('<span class="plan-sub"></span>');
        parts.push('<span class="plan-when"></span>',
                   '<span class="plan-src"></span>');
        if (e.missed_at) {
          parts.push('<span class="pill warn">not in the guide</span>');
        }
        el.innerHTML = parts.join('');
        // textContent, never innerHTML, for anything a third party sent us.
        el.querySelector('.plan-title').textContent = e.title;
        if (e.subtitle) el.querySelector('.plan-sub').textContent = e.subtitle;
        el.querySelector('.plan-when').textContent = e.when;
        el.querySelector('.plan-src').textContent = e.source;
        list.appendChild(el);
      });
      card.hidden = false;
    })
    .catch(function () { /* the page is still useful without this card */ });
})();

// Waving off a suggestion. Only a tip is given a button, and the server
// refuses anything else even if one appeared here by mistake.
//
// Bound in the CAPTURE phase. The notice panel stops propagation on its own
// clicks, so it keeps the panel open while you read it. A delegated listener
// on the bubble phase therefore never fires for a button inside that panel.
// Capture runs on the way down, before the panel can stop anything.
(function () {
  document.addEventListener('click', function (e) {
    var btn = e.target.closest && e.target.closest('[data-dismiss]');
    if (!btn) return;
    e.preventDefault();
    e.stopPropagation();
    btn.disabled = true;
    fetch('/api/notices/' + encodeURIComponent(btn.dataset.dismiss) + '/dismiss',
          {method: 'POST'})
      .then(function (r) {
        if (r.ok) { location.reload(); } else { btn.disabled = false; }
      })
      .catch(function () { btn.disabled = false; });
  }, true);
})();
