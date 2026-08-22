/* The smart filter builder.

   A tree of groups, each holding conditions and more groups, to any depth. The
   shape it edits is exactly the shape the server compiles, so there is no
   translation step to get wrong:

     {op: 'all'|'any'|'none', nodes: [ condition | group, ... ]}
     {field: 'genre', cmp: 'is', value: 'Comedy', blank: false}

   The field list, the comparisons and the values all come from
   /api/filter/fields. Nothing about what can be asked is written twice.

   It lives here rather than inside a template because the add panel and the
   edit panel both use it, and the source picker beside it already taught us
   what happens when a control gets copied: the copies drift. */
(function (w) {
  'use strict';
  var esc = w.CE.esc;

  var META = null;          // fields, comparisons and values, fetched once

  function load() {
    if (META) return Promise.resolve(META);
    return fetch('/api/filter/fields')
      .then(function (r) { return r.json(); })
      .then(function (d) { META = d.ok ? d : null; return META; });
  }

  function fieldMeta(id) {
    if (!META) return null;
    for (var i = 0; i < META.fields.length; i++) {
      if (META.fields[i].id === id) return META.fields[i];
    }
    return null;
  }

  function firstCmp(kind) {
    var list = (META.comparisons || {})[kind] || [];
    return list.length ? list[0].value : 'is';
  }

  function newCondition() {
    var f = META.fields[0];
    return {field: f.id, cmp: firstCmp(f.kind), value: '', blank: false};
  }

  function newGroup() {
    return {op: 'all', nodes: [newCondition()]};
  }

  /* ------------------------------------------------------------- rendering */

  function optionList(items, chosen) {
    var h = '';
    for (var i = 0; i < items.length; i++) {
      var it = items[i];
      var v = (typeof it === 'object') ? it.value : it;
      var l = (typeof it === 'object') ? it.label : it;
      h += '<option value="' + esc(v) + '"' +
           (String(v) === String(chosen) ? ' selected' : '') + '>' + esc(l) + '</option>';
    }
    return h;
  }

  function valueControl(node, path) {
    var f = fieldMeta(node.field);
    if (!f) return '';
    if (f.kind === 'bool') return '';
    if (f.values) {
      var vals = (META.values || {})[f.values] || [];
      if (!vals.length) {
        return '<input class="sfval" data-path="' + path + '" data-k="value" ' +
               'value="' + esc(node.value == null ? '' : node.value) + '" ' +
               'placeholder="nothing in the guide yet">';
      }
      return '<select class="sfval" data-path="' + path + '" data-k="value">' +
             optionList(vals, node.value) + '</select>';
    }
    var type = f.kind === 'number' ? 'number' : f.kind === 'date' ? 'date' : 'text';
    return '<input class="sfval" type="' + type + '" data-path="' + path +
           '" data-k="value" value="' + esc(node.value == null ? '' : node.value) +
           '" placeholder="' + (f.kind === 'date' ? '' : 'value') + '">';
  }

  function conditionHtml(node, path) {
    var f = fieldMeta(node.field) || {kind: 'text'};
    var h = '<div class="sfrow" data-path="' + path + '">';
    h += '<select class="sffield" data-path="' + path + '" data-k="field">' +
         optionList(META.fields.map(function (x) {
           return {value: x.id, label: x.label};
         }), node.field) + '</select>';
    h += '<select class="sfcmp" data-path="' + path + '" data-k="cmp">' +
         optionList((META.comparisons || {})[f.kind] || [], node.cmp) + '</select>';
    h += valueControl(node, path);
    /* Blank is offered only where it changes the answer. A boolean expression
       is never blank, and neither is a field the guide always fills. */
    if (f.kind !== 'bool') {
      h += '<label class="sfblank" title="An item the guide gives no value for. ' +
           'Without this, it does not match either way."><input type="checkbox" ' +
           'data-path="' + path + '" data-k="blank"' + (node.blank ? ' checked' : '') +
           '>or blank</label>';
    }
    h += '<button type="button" class="tiny danger sfx" data-path="' + path +
         '" data-act="del" aria-label="Remove this condition">&times;</button>';
    return h + '</div>';
  }

  function groupHtml(node, path, depth) {
    var h = '<div class="sfgroup" data-path="' + path + '">';
    h += '<div class="sfhead"><span class="sfmatch">Match</span>' +
         '<select data-path="' + path + '" data-k="op">' +
         optionList([{value: 'all', label: 'all of these'},
                     {value: 'any', label: 'any of these'},
                     {value: 'none', label: 'none of these'}], node.op) + '</select>';
    if (depth > 0) {
      h += '<button type="button" class="tiny danger sfdel" data-path="' + path +
           '" data-act="del">Remove group</button>';
    }
    h += '</div>';

    var kids = node.nodes || [];
    if (!kids.length) {
      h += '<div class="sfempty">Nothing here yet, so this group matches nothing.</div>';
    }
    for (var i = 0; i < kids.length; i++) {
      var p = path + '.' + i;
      h += kids[i].op ? groupHtml(kids[i], p, depth + 1) : conditionHtml(kids[i], p);
    }
    h += '<div class="sfacts">' +
         '<button type="button" class="tiny" data-path="' + path +
         '" data-act="addcond">+ Condition</button>' +
         '<button type="button" class="tiny" data-path="' + path +
         '" data-act="addgroup">+ Group</button></div>';
    return h + '</div>';
  }

  /* ---------------------------------------------------------------- the API */

  /* state.tree is the tree this edits in place. onChange runs after every
     edit, so the caller can re-count and repaint its own verdict. */
  function SmartFilter(state, onChange) {
    this.state = state;
    this.onChange = onChange || function () {};
    if (!this.state.tree) this.state.tree = null;
  }

  SmartFilter.prototype.ready = function () {
    var self = this;
    return load().then(function () {
      if (!META) return META;
      if (!self.state.tree) {
        self.state.tree = newGroup();
      } else if (!self.state.tree.op) {
        /* A saved filter may be a single condition with no group around it.
           The server compiles that happily, so it is a real shape and not a
           mistake, but the builder edits groups. Wrapping it changes nothing
           about what it matches. */
        self.state.tree = {op: 'all', nodes: [self.state.tree]};
      }
      return META;
    });
  };

  SmartFilter.prototype.html = function () {
    if (!META) return '<div class="sfempty">Loading what the guide can be asked...</div>';
    return '<div id="sfroot">' + groupHtml(this.state.tree, '0', 0) + '</div>';
  };

  SmartFilter.prototype.repaint = function () {
    var root = document.getElementById('sfroot');
    if (!root || !META) return;
    root.innerHTML = groupHtml(this.state.tree, '0', 0);
  };

  /* A path is the route down the tree, "0.2.1". Resolving it here means the
     markup carries no state of its own beyond where it sits. */
  SmartFilter.prototype.at = function (path) {
    var parts = String(path).split('.'), node = this.state.tree;
    for (var i = 1; i < parts.length; i++) node = node.nodes[+parts[i]];
    return node;
  };

  SmartFilter.prototype.parentOf = function (path) {
    var parts = String(path).split('.');
    if (parts.length < 2) return null;
    return {node: this.at(parts.slice(0, -1).join('.')), index: +parts[parts.length - 1]};
  };

  SmartFilter.prototype.wire = function () {
    var self = this, root = document.getElementById('sfroot');
    if (!root || !META) return;

    root.addEventListener('change', function (e) {
      var el = e.target.closest('[data-k]');
      if (!el) return;
      var node = self.at(el.dataset.path), key = el.dataset.k;
      if (key === 'blank') {
        node.blank = el.checked;
      } else if (key === 'field') {
        /* A new field may not answer the old comparison, and its old value is
           meaningless against a different list. Reset both rather than leave a
           row that reads as valid and is not. */
        node.field = el.value;
        var f = fieldMeta(node.field);
        node.cmp = firstCmp(f ? f.kind : 'text');
        node.value = '';
        self.repaint();
      } else {
        node[key] = el.value;
        if (key === 'op') self.repaint();
      }
      self.onChange();
    });

    root.addEventListener('click', function (e) {
      var btn = e.target.closest('[data-act]');
      if (!btn) return;
      var act = btn.dataset.act, path = btn.dataset.path;
      if (act === 'addcond') {
        self.at(path).nodes.push(newCondition());
      } else if (act === 'addgroup') {
        self.at(path).nodes.push(newGroup());
      } else if (act === 'del') {
        var p = self.parentOf(path);
        if (!p) return;
        p.node.nodes.splice(p.index, 1);
      }
      self.repaint();
      self.onChange();
    });
  };

  /* Whether there is anything worth asking the server about. An empty group
     matches nothing, and the server says so, but there is no point counting. */
  SmartFilter.prototype.usable = function () {
    return this.state.tree ? countConditions(this.state.tree) > 0 : false;
  };

  function countConditions(node) {
    if (!node) return 0;
    if (!node.op) return (node.value !== '' || fieldIsBool(node.field)) ? 1 : 0;
    var n = 0;
    (node.nodes || []).forEach(function (k) { n += countConditions(k); });
    return n;
  }

  function fieldIsBool(id) {
    var f = fieldMeta(id);
    return !!(f && f.kind === 'bool');
  }

  w.CE.SmartFilter = SmartFilter;
  w.CE.smartMeta = function () { return META; };
})(window);
