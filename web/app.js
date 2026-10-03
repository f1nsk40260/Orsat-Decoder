/* Orsat-Decoder — interface. Parle au serveur local (orsatdec/app.py) par WebSocket. */
'use strict';
const $ = s => document.querySelector(s);
const el = (tag, props = {}, ...kids) => {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (k === 'class') e.className = v;
    else if (k === 'text') e.textContent = v;
    else if (k.startsWith('on')) e.addEventListener(k.slice(2), v);
    else if (v !== undefined && v !== null && v !== false) e.setAttribute(k, v);
  }
  for (const c of kids) if (c != null) e.append(c);
  return e;
};
const COLORS = ['#C9A24A', '#4FB3BF', '#E07A5F', '#9B8AE6', '#7FB685', '#6FA8DC', '#D98CB3', '#B8C25A'];

const S = {
  ws: null, catalog: { modes: [], presets: [] }, byId: {}, servers: [], current: 0, server: {},
  chans: new Map(), mode: 'psk31',
  view: null,                 // [f0, f1] Hz affichés
  ui: { palette: 'cadran', contrast: 1, floor: 0, mode: 'psk31' },
};

// ------------------------------------------------------------------ connexion
function connect() {
  const ws = new WebSocket(`ws://${location.host}/ws`);
  ws.binaryType = 'arraybuffer';
  S.ws = ws;
  ws.onmessage = ev => {
    if (typeof ev.data !== 'string') return onWfFrame(ev.data);
    let m; try { m = JSON.parse(ev.data); } catch { return; }
    (H[m.t] || (() => {}))(m);
  };
  ws.onclose = () => { setConn('Interface déconnectée du moteur, nouvelle tentative…', 'bad'); setTimeout(connect, 1500); };
}
const send = o => { if (S.ws && S.ws.readyState === 1) S.ws.send(JSON.stringify(o)); };

const H = {
  hello(m) {
    S.catalog = m.catalog; S.byId = Object.fromEntries(m.catalog.modes.map(x => [x.id, x]));
    S.servers = m.servers; S.current = m.current;
    Object.assign(S.ui, m.ui || {}); S.mode = S.ui.mode || 'psk31';
    applyUi();
    for (const c of [...S.chans.keys()]) removeChanCard(c);
    m.channels.forEach(upsertChan);
    renderServers(); renderModes(); renderPresets(); onServer(m.server);
  },
  server(m) { onServer(m.info); },
  servers(m) { S.servers = m.servers; S.current = m.current; renderServers(); },
  chan(m) { upsertChan(m); },
  chan_removed(m) { removeChanCard(m.ch); },
  text(m) { const c = S.chans.get(m.ch); if (c) writeText(c, m.text); },
  msg(m) { const c = S.chans.get(m.ch); if (c) writeMsg(c, m); },
  cstat(m) { const c = S.chans.get(m.ch); if (c) updateStat(c, m); },
};

function setConn(text, cls) { const c = $('#conn'); c.textContent = text; c.className = 'conn ' + (cls || ''); }

function onServer(info) {
  S.server = info || {};
  if (info && info.connected) {
    setConn(info.rx_name ? `${info.name} · ${info.rx_name}` : info.name, 'ok');
    $('#wfOverlay').hidden = true;
    const full = [info.basefreq, info.basefreq + info.total_bandwidth];
    if (!S.view || S.view[0] < full[0] - 1 || S.view[1] > full[1] + 1) S.view = full;
    renderRx(info);
    drawScale(); placeMarkers(); sendView();
  } else {
    setConn(`Connexion à ${info?.name || 'serveur'}…`, '');
    $('#wfOverlay').hidden = false;
    $('#wfOverlay').innerHTML = `<p>Connexion à <b>${esc(info?.url || '')}</b>…<br><small>Vérifiez l'adresse dans les réglages si rien ne vient.</small></p>`;
  }
}

// ------------------------------------------------------------------ serveurs et récepteurs
function renderServers() {
  const sel = $('#serverSel'); sel.replaceChildren();
  S.servers.forEach((s, i) => sel.append(el('option', { value: i, text: s.name || s.url })));
  sel.value = S.current;
  const list = $('#srvList'); list.replaceChildren();
  S.servers.forEach((s, i) => {
    const n = el('input', { value: s.name || '', placeholder: 'Nom' });
    const u = el('input', { value: s.url || '', placeholder: 'http://hôte:port' });
    const del = el('button', { type: 'button', class: 'icon-btn small', title: 'Supprimer', text: '✕' });
    n.onchange = u.onchange = () => { S.servers[i] = { ...S.servers[i], name: n.value.trim(), url: u.value.trim() }; send({ t: 'servers_set', servers: S.servers }); };
    del.onclick = () => { if (S.servers.length > 1) { S.servers.splice(i, 1); send({ t: 'servers_set', servers: S.servers }); } };
    list.append(el('div', { class: 'srv' }, n, u, del));
  });
}
function renderRx(info) {
  const rs = info.receivers || [];
  $('#rxPick').hidden = rs.length < 2;
  const sel = $('#rxSel'); sel.replaceChildren();
  rs.forEach(r => sel.append(el('option', { value: r.id, text: r.name || r.id })));
  if (info.rx) sel.value = info.rx;
}

// ------------------------------------------------------------------ modes et fréquences connues
function norm(s) { return s.toLowerCase().normalize('NFD').replace(/\p{Diacritic}/gu, ''); }
function renderModes() {
  const q = norm($('#modeSearch').value.trim());
  const nav = $('#modeList'); nav.replaceChildren();
  let fam = null;
  for (const m of S.catalog.modes) {
    if (q && !norm(`${m.label} ${m.family} ${m.desc}`).includes(q)) continue;
    if (m.family !== fam) { fam = m.family; nav.append(el('div', { class: 'fam-name', text: fam })); }
    nav.append(el('button', { class: 'mode-btn' + (m.id === S.mode ? ' active' : ''), type: 'button',
      onclick: () => { S.mode = m.id; S.ui.mode = m.id; savePrefs(); renderModes(); } },
      m.label, m.desc ? el('span', { class: 'd', text: m.desc }) : null));
  }
  const m = S.byId[S.mode];
  $('#placeHint').textContent = m ? `Mode choisi : ${m.label}. Cliquez sur un signal dans le waterfall pour ouvrir un canal.` : '';
}
function renderPresets() {
  const menu = $('#presetMenu'); menu.replaceChildren();
  let grp = null;
  for (const p of S.catalog.presets) {
    const label = S.byId[p.mode]?.label || p.mode;
    if (label !== grp) { grp = label; menu.append(el('div', { class: 'grp', text: label })); }
    menu.append(el('button', { type: 'button', onclick: () => { menu.hidden = true; addChannel(p.mode, p.freq, p.params); } },
      el('span', { text: p.label }), el('span', { text: fmtKHz(p.freq) })));
  }
}
function addChannel(mode, freq, params) {
  const s = S.server;
  if (s.basefreq != null && (freq < s.basefreq || freq > s.basefreq + s.total_bandwidth)) {
    toast(`${fmtKHz(freq)} kHz est hors de la bande de ce récepteur (${fmtKHz(s.basefreq)} à ${fmtKHz(s.basefreq + s.total_bandwidth)} kHz).`, 'error');
    return;
  }
  if (!s.local && S.chans.size >= 3)
    toast('Attention : un serveur PhantomSDR-Plus limite en général à 3 auditeurs par adresse (per_ip). Au-delà, le canal peut être refusé. Lancer Orsat-Decoder sur la machine du serveur lève cette limite.', 'error');
  send({ t: 'add', mode, freq, params });
}

// ------------------------------------------------------------------ canaux
function colorFor(id) {
  const used = new Set([...S.chans.values()].map(c => c.color));
  return COLORS.find(c => !used.has(c)) || COLORS[S.chans.size % COLORS.length];
}
function upsertChan(d) {
  let c = S.chans.get(d.id);
  if (!c) {
    c = { ...d, color: colorFor(d.id), lastSlot: null };
    S.chans.set(d.id, c);
    buildCard(c);
  } else {
    Object.assign(c, d);
  }
  refreshCard(c);
  placeMarkers();
  $('#chanEmpty').hidden = S.chans.size > 0;
}
function removeChanCard(id) {
  const c = S.chans.get(id); if (!c) return;
  c.card.remove(); S.chans.delete(id); placeMarkers();
  $('#chanEmpty').hidden = S.chans.size > 0;
}
function buildCard(c) {
  const m = S.byId[c.mode];
  const freq = el('input', { class: 'freq', title: 'Fréquence (kHz) : modifiable', 'aria-label': 'Fréquence en kHz' });
  freq.onchange = () => {
    const v = parseFloat(freq.value.replace(/\s/g, '').replace(',', '.'));
    if (!isNaN(v)) send({ t: 'retune', ch: c.id, freq: v * 1000 });
  };
  freq.onkeydown = e => { if (e.key === 'Enter') freq.blur(); };
  const pause = el('button', { class: 'icon-btn small', type: 'button', title: 'Pause' });
  pause.onclick = () => send({ t: 'pause', ch: c.id, paused: !c.paused });
  const clear = el('button', { class: 'icon-btn small', type: 'button', title: 'Effacer le texte', text: 'Effacer' });
  clear.onclick = () => { c.out.replaceChildren(); c.cur = null; c.table = null; };
  const save = el('button', { class: 'icon-btn small', type: 'button', title: 'Enregistrer le texte', text: 'Enregistrer' });
  save.onclick = () => saveText(c);
  const close = el('button', { class: 'icon-btn small', type: 'button', title: 'Fermer le canal', text: '✕' });
  close.onclick = () => send({ t: 'remove', ch: c.id });
  const meters = el('div', { class: 'meters' });
  const params = el('div', { class: 'params' });
  for (const p of (m.params || [])) {
    const sel = el('select', { 'aria-label': p.label });
    p.opts.forEach(([v, lab], i) => sel.append(el('option', { value: i, text: lab })));
    sel.onchange = () => send({ t: 'retune', ch: c.id, params: { [p.key]: p.opts[+sel.value][0] } });
    params.append(el('label', {}, p.label, sel));
    (c.paramSel ||= {})[p.key] = [sel, p];
  }
  const out = el('div', { class: 'out', tabindex: '0' });
  const card = el('article', { class: 'chan', style: `--c:${c.color}` },
    el('header', {}, el('span', { class: 'mname', text: m.label }), freq, el('span', { class: 'unit', text: 'kHz' }),
      el('span', { class: 'state' }), el('div', { class: 'tools' }, pause, clear, save, close)),
    el('div', { class: 'sub' }, params, meters), out);
  Object.assign(c, { card, out, freqIn: freq, meters, pauseBtn: pause, stateEl: card.querySelector('.state') });
  $('#chans').prepend(card);
}
function refreshCard(c) {
  if (document.activeElement !== c.freqIn) c.freqIn.value = fmtKHz(c.freq);
  c.stateEl.textContent = c.paused ? 'en pause' : c.state;
  c.pauseBtn.textContent = c.paused ? 'Reprendre' : 'Pause';
  c.card.classList.toggle('paused', !!c.paused);
  for (const [key, [sel, p]] of Object.entries(c.paramSel || {})) {
    const i = p.opts.findIndex(o => o[0] === c.params[key]);
    if (i >= 0) sel.value = i;
  }
}
function updateStat(c, s) {
  const parts = [];
  if (s.snr != null && isFinite(s.snr)) parts.push(`S/B <b>${Math.round(s.snr)} dB</b>`);
  if (s.quality != null) parts.push(`qualité <b>${Math.round(s.quality * 100)} %</b>`);
  if (s.wpm != null) parts.push(`<b>${Math.round(s.wpm)}</b> mpm`);
  if (s.sync != null) parts.push(s.sync ? 'synchro <b>oui</b>' : 'synchro non');
  if (s.last != null) parts.push(`<b>${s.last}</b> décodés`);
  if (s.ready === false) parts.push('<b>décodeur absent</b>');
  c.meters.innerHTML = parts.join(' &nbsp; ');
}
const CALL_RE = /\b((?:[A-Z]{1,2}|[0-9][A-Z]|[A-Z][0-9])[0-9][A-Z]{1,4}(?:\/[A-Z0-9]{1,4})?)\b/g;
const esc = s => String(s).replace(/[&<>]/g, ch => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;' }[ch]));
function writeText(c, text) {
  const out = c.out;
  const atBottom = out.scrollHeight - out.scrollTop - out.clientHeight < 40;
  for (const ch of text) {
    if (ch === '\n' && c.lastCR) { c.lastCR = false; continue; }
    c.lastCR = ch === '\r';
    if (!c.cur || ch === '\r' || ch === '\n') {
      if (c.cur) c.cur.innerHTML = esc(c.cur.textContent).replace(CALL_RE, '<span class="call">$1</span>');
      c.cur = el('div'); out.append(c.cur);
      while (out.childElementCount > 2000) out.firstChild.remove();
      if (ch === '\r' || ch === '\n') continue;
    }
    if (ch === '\b') { c.cur.textContent = c.cur.textContent.slice(0, -1); continue; }
    c.cur.textContent += ch;
  }
  if (atBottom) out.scrollTop = out.scrollHeight;
}
function writeMsg(c, m) {
  const out = c.out;
  const atBottom = out.scrollHeight - out.scrollTop - out.clientHeight < 40;
  if (!c.table) { c.table = el('table', { class: 'msgs' }); out.append(c.table); }
  if (m.error) { toast(m.text, 'error'); return; }
  const utc = m.utc ? `${m.utc.slice(0, 2)}:${m.utc.slice(2, 4)}:${m.utc.slice(4, 6)}` : '';
  const tr = el('tr', { class: (c.lastSlot !== m.utc ? 'slot ' : '') + (/^CQ\b/.test(m.text) ? 'cq' : '') },
    el('td', { class: 'n', text: utc }), el('td', { class: 'n', text: m.snr != null ? (m.snr > 0 ? '+' : '') + Math.round(m.snr) : '' }),
    el('td', { class: 'n', text: m.dt != null ? m.dt.toFixed(1) : '' }), el('td', { class: 'n', text: m.freq ?? '' }),
    el('td', { class: 'm', text: m.text }));
  c.lastSlot = m.utc;
  c.table.append(tr);
  while (c.table.rows.length > 1500) c.table.deleteRow(0);
  if (atBottom) out.scrollTop = out.scrollHeight;
}
function saveText(c) {
  const blob = new Blob([c.out.innerText], { type: 'text/plain;charset=utf-8' });
  const m = S.byId[c.mode];
  const a = el('a', { href: URL.createObjectURL(blob), download: `orsat-${m.id}-${Math.round(c.freq / 1000)}kHz-${new Date().toISOString().slice(0, 16).replace(/[:T]/g, '-')}.txt` });
  document.body.append(a); a.click(); a.remove();
}

// ------------------------------------------------------------------ waterfall
const WF = { c: null, ctx: null, w: 0, h: 0, dpr: 1, scaleH: 0, lut: null, floor: null, peak: null };
const PAL = {
  cadran: [[0, '#0D151D'], [0.28, '#123248'], [0.52, '#1E6B73'], [0.74, '#C9A24A'], [0.9, '#F2D58C'], [1, '#FFFFFF']],
  glace: [[0, '#0B1220'], [0.35, '#16406E'], [0.65, '#3FA3D8'], [0.88, '#BDEBFF'], [1, '#FFFFFF']],
  braise: [[0, '#0E0B10'], [0.3, '#3B1630'], [0.55, '#8E2F2B'], [0.78, '#E07A2E'], [0.92, '#FFD27A'], [1, '#FFFFFF']],
  gris: [[0, '#0A0A0A'], [1, '#FFFFFF']],
};
function buildLut() {
  const cv = document.createElement('canvas'); cv.width = 256; cv.height = 1;
  const g = cv.getContext('2d'); const gr = g.createLinearGradient(0, 0, 256, 0);
  for (const [o, col] of PAL[S.ui.palette] || PAL.cadran) gr.addColorStop(o, col);
  g.fillStyle = gr; g.fillRect(0, 0, 256, 1); WF.lut = g.getImageData(0, 0, 256, 1).data;
}
function wfResize() {
  const r = WF.c.getBoundingClientRect(); WF.dpr = devicePixelRatio || 1;
  const w = Math.max(100, Math.round(r.width * WF.dpr)), h = Math.max(100, Math.round(r.height * WF.dpr));
  if (w === WF.w && h === WF.h) return;
  WF.w = WF.c.width = w; WF.h = WF.c.height = h; WF.scaleH = Math.round(24 * WF.dpr);
  WF.ctx.fillStyle = '#0D151D'; WF.ctx.fillRect(0, 0, w, h);
  drawScale(); placeMarkers();
}
function fmtKHz(f) { return (f / 1000).toLocaleString('fr-FR', { minimumFractionDigits: 1, maximumFractionDigits: 3 }); }
function niceStep(span, px) {
  const target = span / Math.max(2, px / 110);
  const p = Math.pow(10, Math.floor(Math.log10(target)));
  for (const k of [1, 2, 2.5, 5, 10]) if (k * p >= target) return k * p;
  return 10 * p;
}
function drawScale() {
  if (!S.view || !WF.w) return;
  const { ctx, w, scaleH, dpr } = WF, [f0, f1] = S.view;
  ctx.fillStyle = '#141E28'; ctx.fillRect(0, 0, w, scaleH);
  const step = niceStep(f1 - f0, w / dpr);
  ctx.font = `${11 * dpr}px Ubuntu, Cantarell, sans-serif`; ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
  for (let f = Math.ceil(f0 / step) * step; f <= f1; f += step) {
    const x = Math.round((f - f0) / (f1 - f0) * w) + .5;
    ctx.strokeStyle = '#5E7487'; ctx.beginPath(); ctx.moveTo(x, scaleH); ctx.lineTo(x, scaleH - 6 * dpr); ctx.stroke();
    ctx.fillStyle = '#8394A5'; ctx.fillText(fmtKHz(f), x, scaleH / 2 - 2 * dpr);
  }
  ctx.strokeStyle = '#2C3C4C'; ctx.beginPath(); ctx.moveTo(0, scaleH - .5); ctx.lineTo(w, scaleH - .5); ctx.stroke();
}
function onWfFrame(buf) {
  if (!S.view || !WF.w) return;
  const dv = new DataView(buf);
  if (dv.getUint8(0) !== 1) return;
  const lf0 = dv.getFloat64(1, true), lf1 = dv.getFloat64(9, true);
  const bins = new Uint8Array(buf, 17);
  const n = bins.length, { ctx, w, h, scaleH } = WF, [v0, v1] = S.view;
  const row = new Float32Array(w);
  for (let x = 0; x < w; x++) {
    const fa = v0 + x / w * (v1 - v0), fb = v0 + (x + 1) / w * (v1 - v0);
    let a = Math.floor((fa - lf0) / (lf1 - lf0) * n), b = Math.ceil((fb - lf0) / (lf1 - lf0) * n) - 1;
    if (b < a) b = a;
    let mx = -1;
    for (let i = Math.max(0, a); i <= Math.min(n - 1, b); i++) if (bins[i] > mx) mx = bins[i];
    row[x] = mx;
  }
  const valid = Array.from(row).filter(v => v >= 0).sort((p, q) => p - q);
  if (!valid.length) return;
  const fl = valid[Math.floor(valid.length * .3)], pk = valid[Math.floor(valid.length * .995)];
  WF.floor = WF.floor == null ? fl : WF.floor * .9 + fl * .1;
  WF.peak = WF.peak == null ? pk : WF.peak * .9 + pk * .1;
  WF.lastRow = row;
  ctx.drawImage(WF.c, 0, scaleH, w, h - scaleH - 1, 0, scaleH + 1, w, h - scaleH - 1);
  const img = ctx.createImageData(w, 1), lut = WF.lut;
  const span = Math.max(6, WF.peak - WF.floor + 4) / (S.ui.contrast || 1), base = WF.floor + (S.ui.floor || 0) * .5;
  for (let x = 0; x < w; x++) {
    let t = row[x] < 0 ? 0 : (row[x] - base) / span; t = t < 0 ? 0 : t > 1 ? 1 : t;
    const k = (t * 255 | 0) * 4, i = x * 4;
    img.data[i] = lut[k]; img.data[i + 1] = lut[k + 1]; img.data[i + 2] = lut[k + 2]; img.data[i + 3] = 255;
  }
  ctx.putImageData(img, 0, scaleH);
}
function freqAtX(clientX) {
  const r = WF.c.getBoundingClientRect(), [f0, f1] = S.view;
  return f0 + (clientX - r.left) / r.width * (f1 - f0);
}
function xOfFreq(f) { const r = WF.c.getBoundingClientRect(), [f0, f1] = S.view; return (f - f0) / (f1 - f0) * r.width; }
function snapFreq(f, bw, fsk) {
  // PSK, CW : pic le plus proche. FSK (RTTY, Navtex) : centre de gravité des deux tonalités.
  const row = WF.lastRow; if (!row) return f;
  const [f0, f1] = S.view, w = row.length, win = Math.max(bw * .6, (f1 - f0) / w * 4);
  const xa = Math.max(0, Math.floor((f - win - f0) / (f1 - f0) * w)), xb = Math.min(w - 1, Math.ceil((f + win - f0) / (f1 - f0) * w));
  let bx = -1, bv = -1;
  for (let x = xa; x <= xb; x++) if (row[x] > bv) { bv = row[x]; bx = x; }
  if (bx < 0 || bv < WF.floor + 10) return f;
  if (!fsk) return f0 + (bx + .5) / w * (f1 - f0);
  let sw = 0, sx = 0;
  const thr = WF.floor + (bv - WF.floor) * .5;
  for (let x = xa; x <= xb; x++) if (row[x] > thr) { const p = row[x] - thr; sw += p; sx += p * (x + .5); }
  return sw ? f0 + sx / sw / w * (f1 - f0) : f;
}
function placeMarkers() {
  const box = $('#markers'); box.replaceChildren();
  if (!S.view || !WF.c) return;
  for (const c of S.chans.values()) {
    const m = S.byId[c.mode]; if (!m) continue;
    let a, b;
    if (m.whole) { a = c.freq + 200; b = c.freq + 3000; }
    else { a = c.freq - c.bw / 2; b = c.freq + c.bw / 2; }
    const xa = xOfFreq(a), xb = xOfFreq(b);
    if (xb < 0 || xa > WF.c.getBoundingClientRect().width) continue;
    const width = Math.max(4, xb - xa);
    const mk = el('div', { class: 'marker', style: `left:${xa}px;width:${width}px`, title: `${m.label} ${fmtKHz(c.freq)} kHz : glisser pour réaccorder` },
      el('div', { class: 'band', style: `background:${c.color}` }),
      el('div', { class: 'edge', style: `left:0;border-color:${c.color}` }),
      el('div', { class: 'edge', style: `right:0;border-color:${c.color}` }),
      el('div', { class: 'tag', style: `background:${c.color}`, text: m.label }));
    mk.addEventListener('mousedown', e => startDrag(e, c, mk));
    box.append(mk);
  }
}
function startDrag(e, c, mk) {
  e.preventDefault(); e.stopPropagation();
  const x0 = e.clientX, f0 = c.freq, left0 = parseFloat(mk.style.left);
  const [v0, v1] = S.view, perPx = (v1 - v0) / WF.c.getBoundingClientRect().width;
  const move = ev => { mk.style.left = (left0 + ev.clientX - x0) + 'px'; };
  const up = ev => {
    removeEventListener('mousemove', move); removeEventListener('mouseup', up);
    const df = (ev.clientX - x0) * perPx;
    if (Math.abs(ev.clientX - x0) > 2) send({ t: 'retune', ch: c.id, freq: Math.round(f0 + df) });
  };
  addEventListener('mousemove', move); addEventListener('mouseup', up);
}
let viewTimer = null;
function sendView() {
  clearTimeout(viewTimer);
  viewTimer = setTimeout(() => S.view && send({ t: 'view', f0: S.view[0], f1: S.view[1] }), 120);
}
function setView(f0, f1) {
  const s = S.server; if (s.basefreq == null) return;
  const lo = s.basefreq, hi = s.basefreq + s.total_bandwidth, minSpan = 3000;
  let span = Math.max(minSpan, Math.min(hi - lo, f1 - f0));
  f0 = Math.max(lo, Math.min(hi - span, f0)); f1 = f0 + span;
  S.view = [f0, f1];
  WF.ctx.fillStyle = '#0D151D'; WF.ctx.fillRect(0, WF.scaleH, WF.w, WF.h - WF.scaleH);
  WF.floor = WF.peak = null;
  drawScale(); placeMarkers(); sendView();
}
function zoom(factor, center) {
  if (!S.view) return;
  const [f0, f1] = S.view, c = center ?? (f0 + f1) / 2, span = (f1 - f0) * factor;
  setView(c - (c - f0) * factor, c - (c - f0) * factor + span);
}
function initWaterfall() {
  WF.c = $('#wf'); WF.ctx = WF.c.getContext('2d'); buildLut();
  new ResizeObserver(wfResize).observe(WF.c);
  const wrap = $('#wfWrap'), hover = $('#wfHover');
  let drag = null;
  wrap.addEventListener('mousedown', e => {
    if (e.target.closest('.marker, .zoom, .wf-overlay')) return;
    drag = { x: e.clientX, view: S.view && [...S.view], moved: false };
  });
  addEventListener('mousemove', e => {
    if (drag && S.view) {
      const dx = e.clientX - drag.x;
      if (Math.abs(dx) > 3) {
        drag.moved = true;
        const per = (drag.view[1] - drag.view[0]) / WF.c.getBoundingClientRect().width;
        S.view = [drag.view[0] - dx * per, drag.view[1] - dx * per];
        drawScale(); placeMarkers();
      }
    }
  });
  addEventListener('mouseup', e => {
    if (!drag) return;
    const d = drag; drag = null;
    if (d.moved) { setView(S.view[0], S.view[1]); return; }
    if (!S.view || e.target.closest('.marker, .zoom, .wf-overlay') || !e.target.closest('#wfWrap')) return;
    const m = S.byId[S.mode]; if (!m) return;
    let f = freqAtX(e.clientX);
    if (m.whole) {
      const near = S.catalog.presets.filter(p => p.mode === m.id).map(p => p.freq).find(p => f >= p - 500 && f <= p + 3500);
      f = near ?? Math.round(f - 1500);
    } else {
      const fsk = m.bw_from === 'shift' || m.id === 'navtex';
      f = Math.round(snapFreq(f, fsk ? 400 : (m.bw || 200), fsk));
    }
    addChannel(m.id, f);
  });
  wrap.addEventListener('mousemove', e => {
    if (!S.view) return;
    const r = wrap.getBoundingClientRect();
    hover.style.display = 'block'; hover.style.left = (e.clientX - r.left) + 'px';
    hover.textContent = `${fmtKHz(freqAtX(e.clientX))} kHz`;
  });
  wrap.addEventListener('mouseleave', () => { hover.style.display = 'none'; });
  wrap.addEventListener('wheel', e => { e.preventDefault(); zoom(e.deltaY < 0 ? 0.7 : 1 / 0.7, freqAtX(e.clientX)); }, { passive: false });
  $('#zoomIn').onclick = () => zoom(0.5);
  $('#zoomOut').onclick = () => zoom(2);
  $('#zoomAll').onclick = () => { const s = S.server; if (s.basefreq != null) setView(s.basefreq, s.basefreq + s.total_bandwidth); };
}

// ------------------------------------------------------------------ préférences, réglages, divers
let prefT = null;
function savePrefs() { clearTimeout(prefT); prefT = setTimeout(() => send({ t: 'prefs', prefs: S.ui }), 300); }
function applyUi() {
  $('#setPalette').value = S.ui.palette; $('#setContrast').value = S.ui.contrast; $('#setFloor').value = S.ui.floor; buildLut();
}
function toast(text, cls = '') {
  const t = el('div', { class: 'toast ' + cls, role: 'status', text }); $('#toasts').append(t);
  setTimeout(() => t.remove(), cls === 'error' ? 6000 : 3500);
}
function wire() {
  $('#modeSearch').addEventListener('input', renderModes);
  $('#serverSel').onchange = e => send({ t: 'select_server', index: +e.target.value });
  $('#rxSel').onchange = e => send({ t: 'select_rx', rx: e.target.value });
  $('#presetBtn').onclick = e => { e.stopPropagation(); $('#presetMenu').hidden = !$('#presetMenu').hidden; };
  document.addEventListener('click', e => { if (!e.target.closest('.menu-wrap')) $('#presetMenu').hidden = true; });
  $('#settingsBtn').onclick = () => $('#settings').showModal();
  $('#srvAdd').onclick = () => { S.servers.push({ name: 'Nouveau', url: 'http://' }); renderServers(); };
  $('#setPalette').onchange = e => { S.ui.palette = e.target.value; buildLut(); savePrefs(); };
  $('#setContrast').oninput = e => { S.ui.contrast = +e.target.value; savePrefs(); };
  $('#setFloor').oninput = e => { S.ui.floor = +e.target.value; savePrefs(); };
  $('#quitBtn').onclick = () => { if (confirm('Quitter Orsat-Decoder ?')) { send({ t: 'quit' }); setTimeout(() => window.close(), 500); } };
  addEventListener('resize', placeMarkers);
}
wire(); initWaterfall(); connect();
