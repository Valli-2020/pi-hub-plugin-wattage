// wattage — power graph.  Runs sandboxed (opaque origin, no network, no
// storage); data comes only through ph.call('history/<range>').  Text from the
// hub (meter names) is only ever set with textContent, never innerHTML.
(function () {
  'use strict';
  var TAB = ph.surface === 'tab';
  var H = TAB ? 300 : 170;                     // canvas height (CSS px)
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

  // ── layout ──────────────────────────────────────────────────────────────
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
  var legend = el('div', 'legend'); document.body.appendChild(legend);
  var wrap = el('div', 'wrap'); document.body.appendChild(wrap);
  var canvas = el('canvas'); canvas.style.height = H + 'px'; wrap.appendChild(canvas);
  var tip = el('div', 'tip'); wrap.appendChild(tip);
  var empty = el('div', 'empty', 'Collecting samples… the first points appear after one poll interval.');
  wrap.appendChild(empty);

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
    draw();
  }

  function poll() {
    if (busy || !ph.visible) return;
    busy = true;
    var want = range;
    ph.call('history/' + want).then(function (d) { if (want === range && d && d.series) render(d); })
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
})();
