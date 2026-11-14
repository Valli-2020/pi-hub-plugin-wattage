// wattage — power graph.  Runs sandboxed (opaque origin, no network, no
// storage); data comes only through ph.call('history/<range>') and
// ph.call('details').  Text from the
// hub (meter names) is only ever set with textContent, never innerHTML.
(function () {
  'use strict';
  // ph.surface is only set once the dashboard's init message arrives, which
  // is AFTER this script runs: decide tab vs widget in whenSurface(), not here.
  var TAB = false;
  var H = 300;                                 // canvas height of the tab chart (CSS px)
  var PAD = { l: 44, r: 10, t: 10, b: 22 };
  var range = '1h', data = null, busy = false, hover = -1;

  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }
  function v(name, fb) { var t = ph.theme && ph.theme.vars; return (t && t[name]) || fb; }
  function fmtW(w) { return w == null ? '—' : (w < 100 ? w.toFixed(1) : w.toFixed(0)) + ' W'; }
  function hhmm(t) {
    var d = new Date(t * 1000);
    return ('0' + d.getHours()).slice(-2) + ':' + ('0' + d.getMinutes()).slice(-2);
  }
  function colour(i, key) {
    if (key === 'total') return v('--accent', '#b45309');
    if (key === 'hosts') return v('--muted', '#6b7280');
    return [v('--ok', '#15803d'), v('--warn', '#b45309'), '#2563eb', '#9333ea', '#0891b2'][i % 5];
  }
  function niceMax(m) {
    if (!(m > 0)) return 10;
    var p = Math.pow(10, Math.floor(Math.log10(m))), n = m / p;
    return (n <= 1 ? 1 : n <= 2 ? 2 : n <= 2.5 ? 2.5 : n <= 5 ? 5 : 10) * p;
  }

  function whenSurface(cb) {
    var n = 0;
    (function check() {
      if (ph.surface || n++ > 150) cb(ph.surface); else setTimeout(check, 20);   // up to ~3 s
    })();
  }
  whenSurface(function (sf) { TAB = sf !== 'widget'; if (TAB) tab(); else compact(); });

  // ── compact widget (Hosts view): one slim row, total over the last hour ──
  function compact() {
    document.body.className = 'compact';
    var SH = 30, pts = [], hov = -1, cbusy = false;
    var row = el('div', 'crow');
    row.appendChild(el('span', 'clabel', 'Power'));
    var val = el('span', 'cval', '—'); row.appendChild(val);
    var cw = el('div', 'cwrap'); var cv = el('canvas'); cv.style.height = SH + 'px'; cw.appendChild(cv);
    row.appendChild(cw);
    var rng = el('span', 'crange', ''); row.appendChild(rng);
    document.body.appendChild(row);

    function last() { for (var i = pts.length - 1; i >= 0; i--) if (pts[i][1] != null) return pts[i]; return null; }
    function cdraw() {
      var dpr = window.devicePixelRatio || 1, w = cv.clientWidth || 200;
      cv.width = Math.round(w * dpr); cv.height = Math.round(SH * dpr);
      var g = cv.getContext('2d'); g.setTransform(dpr, 0, 0, dpr, 0, 0); g.clearRect(0, 0, w, SH);
      var vals = pts.filter(function (p) { return p[1] != null; }).map(function (p) { return p[1]; });
      var cur = hov >= 0 && pts[hov] ? pts[hov] : last();
      val.textContent = cur ? fmtW(cur[1]) : '—';
      val.title = cur && hov >= 0 ? hhmm(cur[0]) : 'now';
      if (vals.length < 2) { rng.textContent = vals.length ? '' : 'collecting…'; return; }
      var lo = Math.min.apply(null, vals), hi = Math.max.apply(null, vals);
      rng.textContent = lo.toFixed(0) + '–' + hi.toFixed(0) + ' W · 1h';
      var pad = Math.max((hi - lo) * 0.15, 1), y0 = lo - pad, y1 = hi + pad;
      var t1 = Date.now() / 1000, t0 = t1 - 3600;
      var X = function (t) { return 1 + (t - t0) / (t1 - t0) * (w - 2); };
      var Y = function (v2) { return 2 + (1 - (v2 - y0) / (y1 - y0)) * (SH - 4); };
      var c = v('--accent', '#b45309');
      g.strokeStyle = c; g.lineWidth = 1.5; g.lineJoin = 'round';
      var segs = [], seg = [];
      pts.forEach(function (p) { if (p[1] == null) { if (seg.length) segs.push(seg); seg = []; } else seg.push(p); });
      if (seg.length) segs.push(seg);
      segs.forEach(function (sp) {
        g.beginPath();
        sp.forEach(function (p, k) { if (k) g.lineTo(X(p[0]), Y(p[1])); else g.moveTo(X(p[0]), Y(p[1])); });
        g.stroke();
        if (sp.length > 1) {
          g.lineTo(X(sp[sp.length - 1][0]), SH); g.lineTo(X(sp[0][0]), SH); g.closePath();
          g.globalAlpha = 0.10; g.fillStyle = c; g.fill(); g.globalAlpha = 1;
        }
      });
      var m = cur && cur[1] != null ? cur : null;
      if (m) { g.fillStyle = c; g.beginPath(); g.arc(X(m[0]), Y(m[1]), 2.5, 0, 7); g.fill(); }
    }
    function cpoll() {
      if (cbusy || !ph.visible) return;
      cbusy = true;
      ph.call('history/1h').then(function (d) {
        var tot = d && d.series ? d.series.filter(function (x) { return x.key === 'total'; })[0] : null;
        pts = tot ? tot.points : [];
        if (hov >= pts.length) hov = -1;
        cdraw();
      }).catch(function () {}).then(function () { cbusy = false; });
    }
    cv.addEventListener('mousemove', function (e) {
      if (!pts.length) return;
      var r = cv.getBoundingClientRect(), w = r.width, t1 = Date.now() / 1000, t0 = t1 - 3600;
      var mt = t0 + (e.clientX - r.left) / w * 3600, best = -1, bd = 1e12;
      pts.forEach(function (p, i) { if (p[1] != null && Math.abs(p[0] - mt) < bd) { bd = Math.abs(p[0] - mt); best = i; } });
      if (best !== hov) { hov = best; cdraw(); }
    });
    cv.addEventListener('mouseleave', function () { hov = -1; cdraw(); });
    window.addEventListener('resize', cdraw);
    ph.on('visible', function (vis) { if (vis) cpoll(); });
    ph.on('refresh', cpoll);
    ph.on('theme', cdraw);
    cdraw(); cpoll();
    setInterval(cpoll, 15000);
  }

  // ── Power tab ────────────────────────────────────────────────────────────
  function tab() {
  var head = el('div', 'head');
  var title = el('div', 'title');
  title.appendChild(el('span', 'label', 'Power'));
  var now = el('span', 'now', '—'); title.appendChild(now);
  var sub = el('span', 'sub', ''); title.appendChild(sub);
  head.appendChild(title);
  var seg = el('div', 'seg');
  ['1h', '6h', '24h'].forEach(function (r) {
    var b = el('button', r === range ? 'on' : '', r);
    b.type = 'button';
    b.addEventListener('click', function () {
      range = r;
      Array.prototype.forEach.call(seg.children, function (c) { c.className = c === b ? 'on' : ''; });
      data = null; draw(); poll();
    });
    seg.appendChild(b);
  });
  head.appendChild(seg);
  document.body.appendChild(head);
  var cards = el('div', 'cards'); document.body.appendChild(cards);
  var legend = el('div', 'legend'); document.body.appendChild(legend);
  var wrap = el('div', 'wrap'); document.body.appendChild(wrap);
  var canvas = el('canvas'); canvas.style.height = H + 'px'; wrap.appendChild(canvas);
  var tip = el('div', 'tip'); wrap.appendChild(tip);
  var empty = el('div', 'empty', 'Collecting samples… the first points appear after one poll interval.');
  wrap.appendChild(empty);
  var secMeters = el('div', 'sec'); document.body.appendChild(secMeters);
  var secHosts = el('div', 'sec'); document.body.appendChild(secHosts);

  // ── key figures + tables ────────────────────────────────────────────────
  function dur(sec) {
    if (sec < 90) return Math.round(sec) + ' s';
    if (sec < 5400) return Math.round(sec / 60) + ' min';
    return (sec / 3600).toFixed(sec < 36000 ? 1 : 0) + ' h';
  }
  function kwh(x) { return x == null ? '—' : (x < 10 ? x.toFixed(2) : x < 100 ? x.toFixed(1) : x.toFixed(0)) + ' kWh'; }
  function money(x, cur) { return x == null ? '' : x.toFixed(2) + ' ' + (cur || ''); }
  function card(label, value, sub, title) {
    var c = el('div', 'card');
    c.appendChild(el('div', 'cl', label));
    c.appendChild(el('div', 'cv', value));
    if (sub) c.appendChild(el('div', 'cs', sub));
    if (title) c.title = title;
    cards.appendChild(c);
  }
  function renderStats(st) {
    cards.textContent = '';
    if (!st || st.now == null) return;
    var cur = st.currency;
    card('Now', fmtW(st.now));
    card('Average', fmtW(st.avg), range);
    card('Min / max', (st.min < 100 ? st.min.toFixed(1) : st.min.toFixed(0)) + ' – ' + fmtW(st.max), range);
    card('Energy', kwh(st.kwh), (st.cost != null ? money(st.cost, cur) + ' · ' : '') + 'over ' + dur(st.covered_s),
         st.energy_source === 'counter' ? 'From the Shelly energy counter' : 'Integrated from the samples');
    var d = st.per_day_kwh;
    card('Per day', kwh(d), st.per_day_cost != null ? money(st.per_day_cost, cur) : 'at the ' + range + ' average');
    card('Per month', kwh(d * 30), st.per_day_cost != null ? money(st.per_day_cost * 30, cur) : '30 days');
    card('Per year', kwh(d * 365), st.per_day_cost != null ? money(st.per_day_cost * 365, cur) : '365 days');
  }
  function table(box, title, cols, rows, foot) {
    box.textContent = '';
    if (!rows.length) return;
    box.appendChild(el('h4', null, title));
    var t = el('table'), hr = el('tr');
    cols.forEach(function (c) { var th = el('th', c.num ? 'num' : '', c.label); hr.appendChild(th); });
    var th = el('thead'); th.appendChild(hr); t.appendChild(th);
    var tb = el('tbody');
    rows.concat(foot ? [foot] : []).forEach(function (r, i) {
      var tr = el('tr', foot && i === rows.length ? 'foot' : '');
      cols.forEach(function (c) {
        var val = r[c.key], td = el('td', c.num ? 'num' : '');
        if (val && typeof val === 'object' && val.dot) {
          var dot = el('span', 'sdot ' + val.dot); td.appendChild(dot); td.appendChild(document.createTextNode(val.text));
        } else td.textContent = val == null || val === '' ? '—' : val;
        if (r._err && c.key === 'w') td.title = r._err;
        tr.appendChild(td);
      });
      tb.appendChild(tr);
    });
    t.appendChild(tb);
    var sc = el('div', 'tscroll'); sc.appendChild(t); box.appendChild(sc);
  }
  function renderDetails(d) {
    var ref = d.with_hosts ? d.total : (d.mains_w != null ? d.mains_w : d.hosts_w);
    table(secMeters, 'Shelly meters', [
      { key: 'name', label: 'Meter' }, { key: 'role', label: 'Role' }, { key: 'w', label: 'Power', num: 1 },
      { key: 'v', label: 'Voltage', num: 1 }, { key: 'a', label: 'Current', num: 1 },
      { key: 'kwh', label: 'Counter', num: 1 }, { key: 'temp', label: 'Temp', num: 1 }, { key: 'age', label: 'Updated', num: 1 }
    ], d.meters.map(function (m) {
      return {
        name: { dot: m.on == null ? 'unk' : m.on ? 'ok' : 'off', text: m.name },
        role: m.mains ? 'mains' : 'host ' + m.host,
        w: m.w == null ? (m.err ? 'error' : '—') : fmtW(m.w), _err: m.err,
        v: m.v == null ? '' : m.v.toFixed(1) + ' V', a: m.a == null ? '' : m.a.toFixed(2) + ' A',
        kwh: m.kwh == null ? '' : m.kwh.toFixed(1) + ' kWh', temp: m.temp == null ? '' : m.temp.toFixed(0) + ' °C',
        age: m.age == null ? '' : dur(m.age) + ' ago'
      };
    }));
    var foot = null;
    if (d.rest_w != null) foot = { name: 'Rest (mains − hosts)', source: 'switch, router, disks, …', w: fmtW(d.rest_w),
                                   share: ref ? Math.round(d.rest_w / ref * 100) + ' %' : '' };
    table(secHosts, 'Hosts', [
      { key: 'name', label: 'Host' }, { key: 'source', label: 'Source' }, { key: 'w', label: 'Power', num: 1 },
      { key: 'gpu', label: 'GPU', num: 1 }, { key: 'share', label: 'Share', num: 1 }, { key: 'age', label: 'Updated', num: 1 }
    ], d.hosts.map(function (h) {
      return {
        name: h.name, source: h.source, w: h.w == null ? (h.err ? 'error' : '—') : fmtW(h.w), _err: h.err,
        gpu: h.gpu == null ? '' : fmtW(h.gpu), share: h.w != null && ref ? Math.round(h.w / ref * 100) + ' %' : '',
        age: dur(h.age) + ' ago'
      };
    }), foot);
  }

  // ── drawing ─────────────────────────────────────────────────────────────
  function geometry() {
    var w = canvas.clientWidth || 300;
    var series = data ? data.series : [];
    var max = 0;
    series.forEach(function (s) { s.points.forEach(function (p) { if (p[1] != null && p[1] > max) max = p[1]; }); });
    if (data && data.warn_w && data.warn_w < max * 1.5) max = Math.max(max, data.warn_w);
    if (data && data.bad_w && data.bad_w < max * 1.5) max = Math.max(max, data.bad_w);
    var top = niceMax(max * 1.1);
    var t1 = data ? data.now : Date.now() / 1000, t0 = t1 - (data ? data.range : 3600);
    return {
      w: w, top: top, t0: t0, t1: t1,
      x: function (t) { return PAD.l + (t - t0) / (t1 - t0) * (w - PAD.l - PAD.r); },
      y: function (val) { return PAD.t + (1 - val / top) * (H - PAD.t - PAD.b); }
    };
  }

  function draw() {
    var dpr = window.devicePixelRatio || 1, g0 = geometry(), w = g0.w;
    canvas.width = Math.round(w * dpr); canvas.height = Math.round(H * dpr);
    var g = canvas.getContext('2d');
    g.setTransform(dpr, 0, 0, dpr, 0, 0);
    g.clearRect(0, 0, w, H);
    var series = data ? data.series : [];
    empty.style.display = series.length ? 'none' : 'block';
    var font = '11px ' + (v('--font', '') || 'system-ui, sans-serif');
    g.font = font; g.textBaseline = 'middle';

    // grid + y labels
    g.strokeStyle = v('--border', '#e5e7eb'); g.fillStyle = v('--muted', '#6b7280'); g.lineWidth = 1;
    for (var i = 0; i <= 4; i++) {
      var val = g0.top * i / 4, y = Math.round(g0.y(val)) + 0.5;
      g.beginPath(); g.moveTo(PAD.l, y); g.lineTo(w - PAD.r, y); g.stroke();
      g.textAlign = 'right'; g.fillText(val >= 10 ? val.toFixed(0) : val.toFixed(1), PAD.l - 6, y);
    }
    g.textAlign = 'left'; g.fillText('W', 4, PAD.t);
    // x labels
    var span = g0.t1 - g0.t0, step = span <= 3600 ? 600 : span <= 6 * 3600 ? 3600 : 4 * 3600;
    g.textAlign = 'center'; g.textBaseline = 'top';
    for (var t = Math.ceil(g0.t0 / step) * step; t <= g0.t1; t += step) {
      var x = g0.x(t);
      if (x > PAD.l + 14 && x < w - PAD.r - 14) g.fillText(hhmm(t), x, H - PAD.b + 6);
    }
    // thresholds
    [['warn_w', '--warn', '#b45309'], ['bad_w', '--bad', '#b91c1c']].forEach(function (th) {
      var tv = data && data[th[0]];
      if (!tv || tv > g0.top) return;
      var y = Math.round(g0.y(tv)) + 0.5;
      g.save(); g.setLineDash([4, 4]); g.strokeStyle = v(th[1], th[2]); g.globalAlpha = 0.7;
      g.beginPath(); g.moveTo(PAD.l, y); g.lineTo(w - PAD.r, y); g.stroke(); g.restore();
    });
    // series (total last, so it is on top)
    var order = series.slice().sort(function (a, b) { return (a.key === 'total') - (b.key === 'total'); });
    order.forEach(function (s) {
      var c = colour(series.indexOf(s), s.key), gapS = data.range / 60;
      g.save();
      g.strokeStyle = c; g.lineWidth = s.key === 'total' ? 2 : 1.5;
      if (s.key === 'hosts') g.setLineDash([5, 4]);
      var segs = [], cur = [], last = null;
      s.points.forEach(function (p) {
        if (p[1] == null || (last != null && p[0] - last > gapS)) { if (cur.length) segs.push(cur); cur = []; }
        if (p[1] != null) { cur.push(p); last = p[0]; }
      });
      if (cur.length) segs.push(cur);
      segs.forEach(function (pts) {
        g.beginPath();
        pts.forEach(function (p, k) { var x = g0.x(p[0]), y = g0.y(p[1]); if (k) g.lineTo(x, y); else g.moveTo(x, y); });
        g.stroke();
        if (s.key === 'total' && pts.length > 1) {          // soft area under the total
          g.lineTo(g0.x(pts[pts.length - 1][0]), g0.y(0)); g.lineTo(g0.x(pts[0][0]), g0.y(0)); g.closePath();
          g.globalAlpha = 0.12; g.fillStyle = c; g.fill(); g.globalAlpha = 1;
        }
        if (pts.length === 1) { g.fillStyle = c; g.beginPath(); g.arc(g0.x(pts[0][0]), g0.y(pts[0][1]), 2, 0, 7); g.fill(); }
      });
      g.restore();
    });
    // hover
    tip.style.display = 'none';
    if (hover >= 0 && series.length) {
      var ref = series[0].points[hover];
      if (ref) {
        var hx = Math.round(g0.x(ref[0])) + 0.5;
        g.strokeStyle = v('--border-strong', '#9ca3af'); g.lineWidth = 1;
        g.beginPath(); g.moveTo(hx, PAD.t); g.lineTo(hx, H - PAD.b); g.stroke();
        tip.textContent = '';
        tip.appendChild(el('div', 'tt', hhmm(ref[0])));
        series.forEach(function (s, i) {
          var p = s.points[hover], row = el('div', 'tr');
          var dot = el('span', 'dot'); dot.style.background = colour(i, s.key); row.appendChild(dot);
          row.appendChild(el('span', 'tn', s.name));
          row.appendChild(el('span', 'tv', fmtW(p ? p[1] : null)));
          tip.appendChild(row);
        });
        tip.style.display = 'block';
        var left = hx + 12;
        if (left + tip.offsetWidth > w) left = hx - 12 - tip.offsetWidth;
        tip.style.left = Math.max(0, left) + 'px';
      }
    }
  }

  function render(d) {
    data = d;
    var tot = null;
    d.series.forEach(function (s) {
      if (s.key === 'total') for (var i = s.points.length - 1; i >= 0; i--) if (s.points[i][1] != null) { tot = s.points[i][1]; break; }
    });
    now.textContent = fmtW(tot);
    sub.textContent = d.with_hosts ? 'mains + hosts' : 'mains';
    legend.textContent = '';
    if (d.series.length > 1) {
      d.series.forEach(function (s, i) {
        var it = el('span', 'li' + (s.key === 'hosts' ? ' dashed' : ''));
        var dot = el('span', 'dot'); dot.style.background = colour(i, s.key); it.appendChild(dot);
        it.appendChild(document.createTextNode(s.name));
        legend.appendChild(it);
      });
    }
    if (hover >= 0 && (!d.series.length || hover >= d.series[0].points.length)) hover = -1;
    renderStats(d.stats);
    draw();
  }

  function poll() {
    if (busy || !ph.visible) return;
    busy = true;
    var want = range;
    ph.call('history/' + want).then(function (d) { if (want === range && d && d.series) render(d); })
      .then(function () { return ph.call('details'); })
      .then(function (d) { if (d && d.meters) renderDetails(d); })
      .catch(function () { /* transient */ })
      .then(function () { busy = false; });
  }

  canvas.addEventListener('mousemove', function (e) {
    if (!data || !data.series.length) return;
    var g0 = geometry(), r = canvas.getBoundingClientRect(), mx = e.clientX - r.left, best = -1, bd = 1e9;
    data.series[0].points.forEach(function (p, i) { var d = Math.abs(g0.x(p[0]) - mx); if (d < bd) { bd = d; best = i; } });
    if (best !== hover) { hover = best; draw(); }
  });
  canvas.addEventListener('mouseleave', function () { hover = -1; draw(); });
  window.addEventListener('resize', draw);
  ph.on('visible', function (vis) { if (vis) poll(); });
  ph.on('refresh', poll);
  ph.on('theme', draw);
  draw();
  poll();
  setInterval(poll, 15000);
  }
})();
