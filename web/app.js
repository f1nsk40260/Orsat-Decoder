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
const MAX_CH = 3;            // canaux ouverts en même temps (le serveur applique la même limite)
const COLORS = ['#C9A24A', '#4FB3BF', '#E07A5F', '#9B8AE6', '#7FB685', '#6FA8DC', '#D98CB3', '#B8C25A'];

const S = {
  ws: null, bookmarks: [], catalog: { modes: [], presets: [] }, byId: {}, types: {}, sources: [], current: null, server: {},
  inputs: [],
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
    if (typeof ev.data !== 'string') {
      const k = new DataView(ev.data).getUint8(0);
      return k === 2 ? onSpecFrame(ev.data) : k === 3 ? onListenFrame(ev.data) : onWfFrame(ev.data);
    }
    let m; try { m = JSON.parse(ev.data); } catch { return; }
    (H[m.t] || (() => {}))(m);
  };
  ws.onclose = () => { setConn('Interface déconnectée du moteur, nouvelle tentative…', 'bad'); setTimeout(connect, 1500); };
}
const send = o => { if (S.ws && S.ws.readyState === 1) S.ws.send(JSON.stringify(o)); };

const H = {
  hello(m) {
    if (m.version) { $('#ver').textContent = m.version; document.title = `Orsat-Decoder ${m.version}`; }
    S.catalog = m.catalog; S.byId = Object.fromEntries(m.catalog.modes.map(x => [x.id, x]));
    S.types = m.types; S.sources = m.sources; S.current = m.current;
    Object.assign(S.ui, m.ui || {}); S.mode = S.ui.mode && S.ui.mode !== 'ident' ? S.ui.mode : 'psk31';
    applyUi();
    for (const c of [...S.chans.keys()]) removeChanCard(c);
    m.channels.forEach(upsertChan);
    S.bookmarks = m.bookmarks || [];
    renderSources(); renderModes(); renderPresets(); renderRibbon(); onSource(m.source);
  },
  detected(m) { if (S.detectWait) { const f = S.detectWait; S.detectWait = null; f(m); } },
  bookmarks(m) { S.bookmarks = m.list; renderPresets(); renderRibbon(); },
  source(m) { onSource(m.info); },
  sources(m) { S.sources = m.sources; S.current = m.current; renderSources(); },
  chans_reset() { for (const c of [...S.chans.keys()]) removeChanCard(c); },
  chan(m) { upsertChan(m); },
  chan_removed(m) { removeChanCard(m.ch); },
  text(m) { const c = S.chans.get(m.ch); if (c) { writeText(c, m.text); if (m.text.trim()) rxPulse(); } },
  tr(m) { const c = S.chans.get(m.ch); if (c) writeTr(c, m.text); },
  msg(m) { const c = S.chans.get(m.ch); if (c) { writeMsg(c, m); rxPulse(); } },
  img(m) { const c = S.chans.get(m.ch); if (c) { onImage(c, m); rxPulse(); } },
  cstat(m) { const c = S.chans.get(m.ch); if (c) updateStat(c, m); },
  ident(m) { const c = S.chans.get(m.ch); if (c) renderIdent(c, m); },
  notice(m) { toast(m.text, m.level === 'error' ? 'error' : ''); },
  audio_inputs(m) { S.inputs = m.inputs || []; renderSources(); },
};

function setConn(text, cls) {
  const c = $('#conn'); c.textContent = text; c.title = text; c.className = 'conn ' + (cls || '');
  const led = $('#ledSrc'); led.className = 'led' + (cls === 'ok' ? ' on' : cls === 'bad' ? ' bad' : ''); led.title = text;
}
// voyants de l'en-tête : canaux ouverts, activité de décodage ; horloge UTC
function updLeds() { $('#nCh').textContent = S.chans.size; $('#ledCh').classList.toggle('on', S.chans.size > 0); }
let rxTimer = 0;
function rxPulse() {
  $('#ledRx').classList.add('act');
  clearTimeout(rxTimer); rxTimer = setTimeout(() => $('#ledRx').classList.remove('act'), 1500);
}
function tickUtc() {
  const d = new Date();
  $('#utc').textContent = d.toISOString().slice(11, 19);
  $('#loc').textContent = d.toLocaleTimeString('fr-FR', { hour12: false });
}
tickUtc(); setInterval(tickUtc, 1000);

const CODECS = { pcm: 'PCM', flac: 'FLAC', opus: 'Opus' };
function onSource(info) {
  S.server = info || {};
  const s = S.server, shared = !!s.shared;
  if (s.id) { S.current = s.id; $('#sourceSel').value = s.id; }
  $('#dialPick').hidden = !(shared && s.can_qsy);
  if (shared && s.can_qsy && document.activeElement !== $('#dialIn')) $('#dialIn').value = s.dial != null ? fmtKHz(s.dial) : '';
  $('#rxPick').hidden = shared || (s.receivers || []).length < 2;
  if (!shared) renderRx(s);
  if (s.connected) {
    let label = s.name;
    if (s.kind === 'phantom') label += (s.rx_name ? ` · ${s.rx_name}` : '') + (s.codec ? ` · ${CODECS[s.codec] || s.codec}` : '');
    else if (s.kind === 'tci') label += s.device ? ` · ${s.device}` : '';
    else if (s.kind === 'owrx' || s.kind === 'kiwi') label += s.rx_name ? ` · ${s.rx_name}` : '';
    else if (s.nodial) label += ' · sans CAT, fréquences audio';
    setConn(s.error ? `${label} · ${s.error}` : label, s.error ? 'bad' : 'ok');
    $('#wfOverlay').hidden = true;
    const full = [s.basefreq, s.basefreq + s.total_bandwidth];
    // nouvelle source, ou récepteur réaccordé (audio partagé) : on repart sur toute la bande
    const key = `${s.id}|${s.basefreq}|${s.total_bandwidth}`;
    if (!S.view || key !== S.viewKey) {
      S.view = full; S.viewKey = key;
      if (WF.ctx) { WF.ctx.fillStyle = '#0D151D'; WF.ctx.fillRect(0, WF.scaleH, WF.w, WF.h - WF.scaleH); }
      WF.floor = WF.peak = null; WF.lastRow = null; WF.warm = 4;
    }
    drawScale(); placeMarkers(); sendView();
  } else {
    setConn(s.error ? s.error : `Connexion à ${s.name || 'la source'}…`, s.error ? 'bad' : '');
    $('#wfOverlay').hidden = false;
    const where = s.kind === 'audio' ? `l'entrée audio <b>${esc(s.device || 'par défaut')}</b>` : `<b>${esc(s.url || '')}</b>`;
    $('#wfOverlay').innerHTML = `<p>${s.error ? esc(s.error) : 'Connexion à ' + where + '…'}<br><small>Vérifiez la source dans les réglages si rien ne vient.</small></p>`;
  }
  renderModes();
  for (const c of S.chans.values()) refreshCard(c);
}

// ------------------------------------------------------------------ sources et récepteurs
const SRC_FIELDS = {
  phantom: [['url', 'Adresse', 'http://orsat.ddns.net:8080']],
  kiwi: [['url', 'Adresse', 'http://exemple.org:8073'], ['password', 'Mot de passe (facultatif)', '']],
  owrx: [['url', 'Adresse', 'http://exemple.org:8073']],
  tci: [['url', 'Adresse TCI', 'ws://127.0.0.1:50001'], ['trx', 'Récepteur (TRX)', '0']],
  audio: [['device', 'Entrée audio', ''], ['rigctl', 'CAT rigctld (facultatif)', '127.0.0.1:4532']],
};
function renderSources() {
  const sel = $('#sourceSel'); sel.replaceChildren();
  S.sources.forEach(src => sel.append(el('option', { value: src.id, text: src.name || src.url || src.id })));
  sel.append(el('option', { disabled: '', text: '──────────' }),
    el('option', { value: '+add', text: '＋ Ajouter un serveur…' }),
    el('option', { value: '+manage', text: 'Gérer les sources…' }));
  sel.value = S.current;
  const nt = $('#srcNewType');
  if (!nt.childElementCount) for (const [k, v] of Object.entries(S.types)) nt.append(el('option', { value: k, text: v }));
  const list = $('#srcList'); list.replaceChildren();
  S.sources.forEach((src, i) => {
    const commit = () => send({ t: 'sources_set', sources: S.sources });
    const name = el('input', { value: src.name || '', placeholder: 'Nom', 'aria-label': 'Nom de la source' });
    name.onchange = () => { src.name = name.value.trim(); commit(); };
    const del = el('button', { type: 'button', class: 'icon-btn small', title: 'Supprimer cette source', text: '✕' });
    del.onclick = () => { if (S.sources.length > 1) { S.sources.splice(i, 1); commit(); } };
    const box = el('div', { class: 'src' + (src.id === S.current ? ' cur' : '') },
      el('div', { class: 'top' }, name, el('span', { class: 'kind', text: S.types[src.type] || src.type }), del));
    for (const [key, label, ph] of SRC_FIELDS[src.type] || []) {
      let input;
      if (key === 'device') {
        input = el('select', { 'aria-label': label });
        input.append(el('option', { value: '', text: 'Entrée par défaut du système' }));
        const known = new Set();
        for (const d of S.inputs) { known.add(d.name); input.append(el('option', { value: d.name, text: d.desc })); }
        if (src.device && !known.has(src.device)) input.append(el('option', { value: src.device, text: src.device }));
        input.value = src.device || '';
      } else {
        input = el('input', { value: src[key] ?? '', placeholder: ph, 'aria-label': label, spellcheck: 'false',
          ...(key === 'password' ? { type: 'password', autocomplete: 'off' } : {}) });
      }
      input.onchange = () => { src[key] = key === 'trx' ? (parseInt(input.value, 10) || 0) : input.value.trim(); commit(); };
      box.append(el('label', { class: 'f' }, label, input));
    }
    list.append(box);
  });
}
const SRV_LABEL = { phantom: 'PhantomSDR / Orsat-SDR', kiwi: 'KiwiSDR', owrx: 'OpenWebRX' };
function srvAdd() {
  const dlg = $('#srvDialog');
  $('#srvUrl').value = ''; $('#srvName').value = ''; $('#srvPass').value = ''; $('#srvType').value = '';
  $('#srvMsg').textContent = 'Collez l\'adresse de la page web du récepteur. Le type est reconnu tout seul.';
  $('#srvMsg').className = 'hint'; $('#srvPassRow').hidden = true; $('#srvOk').disabled = false;
  $('#srvType').onchange = () => { $('#srvPassRow').hidden = $('#srvType').value !== 'kiwi'; };
  for (const b of dlg.querySelectorAll('.srv-cancel')) b.onclick = () => { S.detectWait = null; dlg.close(); };
  const say = (t, bad) => { $('#srvMsg').textContent = t; $('#srvMsg').className = 'hint' + (bad ? ' bad' : ''); };
  $('#srvForm').onsubmit = e => {
    e.preventDefault();
    let url = $('#srvUrl').value.trim();
    if (!url) return;
    if (!/^[a-z]+:\/\//i.test(url)) url = 'http://' + url;
    const save = (type, name) => {
      const src = { id: Math.random().toString(36).slice(2, 8), type, url,
        name: $('#srvName').value.trim() || name || url.replace(/^[a-z]+:\/\//i, '').replace(/\/$/, '') };
      if (type === 'kiwi') src.password = $('#srvPass').value;
      S.sources.push(src);
      send({ t: 'sources_set', sources: S.sources });
      send({ t: 'select_source', id: src.id });
      dlg.close();
      toast(`Serveur « ${src.name} » (${SRV_LABEL[type]}) enregistré.`);
    };
    const type = $('#srvType').value;
    if (type) { save(type); return; }
    say('Reconnaissance du serveur…'); $('#srvOk').disabled = true;
    S.detectWait = m => {
      $('#srvOk').disabled = false;
      if (m.type === 'websdr')
        return say('C\'est un WebSDR (websdr.org) : son flux audio est dans un format fermé, Orsat-Decoder ne peut pas s\'y connecter. '
          + 'Écoutez-le dans le navigateur et choisissez la source « Entrée audio » sur le « Monitor » de la carte son.', true);
      if (!SRV_LABEL[m.type]) {
        $('#srvPassRow').hidden = false;
        return say('Serveur non reconnu (adresse injoignable, ou type inconnu). Vérifiez l\'adresse, ou choisissez le type ci-dessus.', true);
      }
      save(m.type, m.name);
    };
    send({ t: 'detect', url });
  };
  dlg.showModal();
  $('#srvUrl').focus();
}

function renderRx(info) {
  const rs = info.receivers || [];
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
    if (m.kind === 'ident') continue;               // l'identification a son bouton sous le waterfall
    if (q && !norm(`${m.label} ${m.family} ${m.desc}`).includes(q)) continue;
    if (m.family !== fam) { fam = m.family; nav.append(el('div', { class: 'fam-name', text: fam })); }
    nav.append(el('button', { class: 'mode-btn' + (m.id === S.mode ? ' active' : ''), type: 'button',
      onclick: () => { S.mode = m.id; S.ui.mode = m.id; setArmed(false); savePrefs(); renderModes(); } },
      m.label, m.desc ? el('span', { class: 'd', text: m.desc }) : null));
  }
  const m = S.byId[S.mode];
  const shared = S.server && S.server.shared;
  $('#placeHint').textContent = S.armed ? 'Identification : cliquez sur le signal inconnu dans le waterfall.' : !m ? '' : shared
    ? `Mode choisi : ${m.label}. Cliquez sur un signal dans le waterfall audio : tous les canaux partagent l'audio de la source.`
    : `Mode choisi : ${m.label}. Cliquez sur un signal dans le waterfall pour ouvrir un canal.`;
}
// ------------------------------------------------------------------ signets
function bmSave(list) { S.bookmarks = list; send({ t: 'bookmarks', list }); renderPresets(); renderRibbon(); }
function bmEdit(idx, init) {
  const dlg = $('#bmDialog'), isNew = idx == null;
  const b = isNew ? { label: '', mode: S.mode || 'psk31', freq: 0, ribbon: true, ...(init || {}) } : S.bookmarks[idx];
  $('#bmTitle').textContent = isNew ? 'Nouveau signet' : 'Modifier le signet';
  $('#bmLabel').value = b.label || '';
  $('#bmFreq').value = b.freq ? fmtKHz(b.freq).replace(/\s/g, '') : '';
  const sel = $('#bmMode'); sel.replaceChildren();
  let fam = null, og = null;
  for (const m of S.catalog.modes) {
    if (m.family !== fam) { fam = m.family; og = el('optgroup', { label: fam }); sel.append(og); }
    og.append(el('option', { value: m.id, text: m.label }));
  }
  sel.value = b.mode;
  $('#bmRibbonChk').checked = b.ribbon !== false;
  $('#bmDelete').hidden = isNew;
  $('#bmDelete').onclick = () => { dlg.close(); bmSave(S.bookmarks.filter((_, i) => i !== idx)); };
  // Annuler et ✕ ferment sans valider le formulaire (champs obligatoires vides compris)
  for (const btn of dlg.querySelectorAll('.bm-cancel')) btn.onclick = () => dlg.close();
  $('#bmForm').onsubmit = e => {
    if (e.submitter && e.submitter.value !== 'ok') return;
    const f = parseFloat($('#bmFreq').value.replace(/\s/g, '').replace(',', '.')) * 1000;
    if (!(f > 0)) { e.preventDefault(); $('#bmFreq').focus(); toast('Fréquence invalide.', 'error'); return; }
    const nb = { ...b, label: $('#bmLabel').value.trim() || `${S.byId[sel.value]?.label || sel.value} ${fmtKHz(f)}`,
      mode: sel.value, freq: f, ribbon: $('#bmRibbonChk').checked };
    const list = [...S.bookmarks];
    if (isNew) list.push(nb); else list[idx] = nb;
    bmSave(list);
  };
  $('#presetMenu').hidden = true;
  dlg.showModal();
  $('#bmLabel').focus();
}
function renderPresets() {
  const menu = $('#presetMenu'); menu.replaceChildren();
  const q = el('input', { type: 'search', class: 'dir-search', placeholder: 'Chercher un signet, un signal, un mode ou une fréquence (kHz)…',
    autocomplete: 'off', spellcheck: 'false' });
  const active = S.chans.get(S.active);
  const tools = el('div', { class: 'dir-tools' },
    el('button', { type: 'button', class: 'btn', text: '+ Nouveau signet', onclick: e => { e.stopPropagation(); bmEdit(null); } }),
    el('button', { type: 'button', class: 'btn', text: '+ Canal actif', title: 'Mettre en signet la fréquence et le mode du canal actif',
      disabled: active ? null : 'disabled',
      onclick: e => { e.stopPropagation(); const c = S.chans.get(S.active); if (c) bmEdit(null, { mode: c.mode, freq: c.freq,
        label: `${S.byId[c.mode]?.label || c.mode} ${fmtKHz(c.freq)}` }); } }),
    el('span', { class: 'spacer' }),
    el('button', { type: 'button', class: 'btn', text: 'Signets par défaut', title: 'Remplacer vos signets par la liste d\'origine',
      onclick: e => { e.stopPropagation(); if (confirm('Remplacer tous vos signets par la liste d\'origine ?')) send({ t: 'bookmarks_reset' }); } }));
  const legend = el('div', { class: 'dir-legend' },
    el('span', { class: 'dec', text: 'décodable' }), el('span', { class: 'nodec', text: 'identifiable seulement (ouvre « Identifier »)' }),
    el('span', { class: 'flag', text: '⚑ affiché sous l\'échelle du waterfall' }));
  const body = el('div', { class: 'dir-body' });
  menu.append(el('div', { class: 'dir-head' }, q, tools, legend), body);
  const freqBtn = (f, dec, onclick, title) => el('button', { type: 'button', class: 'fchip ' + (dec ? 'dec' : 'nodec'), title,
    onclick: e => { e.stopPropagation(); menu.hidden = true; onclick(); } }, fmtKHz(f));
  const sections = [];
  // 1. les signets de l'utilisateur
  const mine = el('div', { class: 'dir-sec' }, el('div', { class: 'grp', text: `Mes signets (${S.bookmarks.length})` }));
  const order = S.bookmarks.map((bm, i) => [bm, i]).sort((x, y) =>
    (S.byId[x[0].mode]?.label || '').localeCompare(S.byId[y[0].mode]?.label || '') || x[0].freq - y[0].freq);
  for (const [bm, i] of order) {
    const ml = S.byId[bm.mode]?.label || bm.mode, dec = S.byId[bm.mode]?.kind !== 'ident';
    const flag = el('button', { type: 'button', class: 'bm-flag' + (bm.ribbon !== false ? ' on' : ''),
      title: bm.ribbon !== false ? 'Affiché sous l\'échelle : cliquer pour le retirer' : 'Afficher sous l\'échelle du waterfall', text: '⚑',
      onclick: e => { e.stopPropagation(); const l = [...S.bookmarks]; l[i] = { ...bm, ribbon: bm.ribbon === false }; bmSave(l); } });
    const edit = el('button', { type: 'button', class: 'bm-act', title: 'Modifier', text: '✎', onclick: e => { e.stopPropagation(); bmEdit(i); } });
    const del = el('button', { type: 'button', class: 'bm-act', title: 'Supprimer', text: '✕',
      onclick: e => { e.stopPropagation(); bmSave(S.bookmarks.filter((_, j) => j !== i)); } });
    const row = el('div', { class: 'dir-row bm-row' },
      el('span', { class: 'dir-title' }, el('span', { class: 'dir-name ' + (dec ? 'dec' : 'nodec'), text: bm.label }),
        el('span', { class: 'dir-mode', text: ml })),
      el('span', { class: 'dir-freqs' }, freqBtn(bm.freq, dec, () => usePreset(bm), `Ouvrir ${ml} sur ${fmtKHz(bm.freq)} kHz`),
        el('span', { class: 'spacer' }), flag, edit, del));
    row.dataset.key = (bm.label + ' ' + ml + ' ' + bm.freq / 1000).toLowerCase();
    mine.append(row);
  }
  sections.push(mine);
  // 2. tous les signaux identifiables de la base (on peut en faire des signets)
  const all = el('div', { class: 'dir-sec' }, el('div', { class: 'grp', text: `Signaux identifiables (${(S.catalog.directory || []).length})` }));
  for (const d of S.catalog.directory || []) {
    const dec = !!d.mode;
    const mode = dec ? d.mode : 'ident';
    const ml = dec ? (S.byId[d.mode]?.label || d.mode) : '';
    const name = el('span', { class: 'dir-name ' + (dec ? 'dec' : 'nodec'), text: d.title });
    const freqs = el('span', { class: 'dir-freqs' });
    for (const f of d.freqs) {
      freqs.append(el('span', { class: 'fpair' },
        freqBtn(f, dec, () => usePreset({ mode, freq: f }), dec ? `Ouvrir ${ml} sur ${fmtKHz(f)} kHz` : `Identifier le signal sur ${fmtKHz(f)} kHz`),
        el('button', { type: 'button', class: 'fadd', title: 'Ajouter aux signets', text: '+',
          onclick: e => { e.stopPropagation(); bmEdit(null, { mode, freq: f, label: d.title.slice(0, 60) }); } })));
    }
    if (d.range) freqs.append(el('span', { class: 'frange', text: `${fmtKHz(d.range[0])} à ${fmtKHz(d.range[1])} kHz` }));
    if (!d.freqs.length && !d.range) freqs.append(el('span', { class: 'frange', text: 'fréquence non précisée' }));
    const row = el('div', { class: 'dir-row' }, el('span', { class: 'dir-title' }, name,
      dec ? el('span', { class: 'dir-mode', text: ml }) : null), freqs);
    row.dataset.key = (d.title + ' ' + ml + ' ' + d.freqs.map(f => f / 1000).join(' ')).toLowerCase();
    all.append(row);
  }
  sections.push(all);
  body.append(...sections);
  q.addEventListener('click', e => e.stopPropagation());
  q.addEventListener('input', () => {
    const t = q.value.trim().toLowerCase().replace(',', '.');
    for (const r of body.querySelectorAll('.dir-row')) r.hidden = !!t && !r.dataset.key.includes(t);
    for (const sec of sections) sec.hidden = ![...sec.querySelectorAll('.dir-row')].some(r => !r.hidden);
  });
}
// bande de signets sous l'échelle du waterfall : un clic ouvre le canal
function renderRibbon() {
  const box = $('#bmRibbon'); if (!box) return;
  const W = WF.c ? WF.c.getBoundingClientRect().width : 0;
  // on ne reconstruit que si la vue ou les signets ont changé : sinon le bouton sous la souris
  // disparaîtrait entre l'appui et le relâchement, et le clic serait perdu
  const key = JSON.stringify([S.view, W, S.bookmarks]);
  if (key === S.ribbonKey) return;
  S.ribbonKey = key;
  box.replaceChildren();
  if (!S.view || !WF.c || !S.bookmarks) return;
  const items = S.bookmarks.filter(b => b.ribbon !== false).map(b => ({ b, x: xOfFreq(b.freq) }))
    .filter(o => o.x >= -2 && o.x <= W + 2).sort((p, q) => p.x - q.x);
  let right = -1e9;
  for (const { b, x } of items) {
    const ml = S.byId[b.mode]?.label || b.mode;
    const flag = el('button', { type: 'button', class: 'rb', style: `left:${x}px`,
      title: `${b.label} — ${ml}, ${fmtKHz(b.freq)} kHz : cliquer pour ouvrir`,
      onmousedown: e => { if (e.button !== 0) return; e.stopPropagation(); e.preventDefault(); usePreset(b); } });
    const lbl = el('span', { text: b.label });
    flag.append(lbl);
    box.append(flag);
    const w = lbl.getBoundingClientRect().width + 14;
    if (x < right) flag.classList.add('mini');          // libellés qui se chevauchent : simple repère
    else right = x + w;
  }
}
function usePreset(p) {
  if (S.server.shared) { send({ t: 'preset', mode: p.mode, freq: p.freq, params: p.params }); return; }
  addChannel(p.mode, p.freq, p.params);
}
function addChannel(mode, freq, params, replace) {
  const s = S.server;
  if (!s.shared && s.basefreq != null && (freq < s.basefreq || freq > s.basefreq + s.total_bandwidth)) {
    toast(`${fmtKHz(freq)} kHz est hors de la bande de ce récepteur (${fmtKHz(s.basefreq)} à ${fmtKHz(s.basefreq + s.total_bandwidth)} kHz).`, 'error');
    return;
  }
  if ([...S.chans.keys()].filter(id => id !== replace).length >= MAX_CH) {
    toast(`${MAX_CH} canaux au maximum : fermez-en un pour en ouvrir un autre.`, 'error');
    return;
  }
  send({ t: 'add', mode, freq, params, replace });
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
    updLeds();
    buildCard(c);
  } else {
    const keep = c.pendingFreq != null && Date.now() - (c.lastTune || 0) < 600 ? c.pendingFreq : null;
    Object.assign(c, d);
    if (keep != null) c.freq = keep;           // un écho du moteur ne doit pas faire reculer la molette
  }
  refreshCard(c);
  placeMarkers();
  $('#chanEmpty').hidden = S.chans.size > 0;
}
function removeChanCard(id) {
  const c = S.chans.get(id); if (!c) return;
  if (S.listen === id) { S.listen = null; send({ t: 'listen', ch: null }); }
  c.card.remove(); S.chans.delete(id); updLeds(); placeMarkers();
  $('#chanEmpty').hidden = S.chans.size > 0;
}
function buildCard(c) {
  const m = S.byId[c.mode];
  const freq = el('input', { class: 'freq', 'aria-label': 'Fréquence',
    title: 'Fréquence : molette ou flèches pour accorder (Maj : 1 Hz, Alt : 100 Hz), ou tapez une valeur puis Entrée' });
  const step = dir => e => { e.preventDefault(); setActive(c.id); tuneChan(c, Math.round((c.pendingFreq ?? c.freq) + dir * stepOf(e))); };
  const up = el('button', { class: 'fstep', type: 'button', title: 'Monter (Maj : 1 Hz, Alt : 100 Hz)', 'aria-label': 'Monter la fréquence', text: '▲', onclick: step(1) });
  const down = el('button', { class: 'fstep', type: 'button', title: 'Descendre (Maj : 1 Hz, Alt : 100 Hz)', 'aria-label': 'Descendre la fréquence', text: '▼', onclick: step(-1) });
  const wheelIco = el('span', { class: 'fwheel', 'aria-hidden': 'true' });
  wheelIco.innerHTML = '<svg viewBox="0 0 16 22"><rect x="1.5" y="1.5" width="13" height="19" rx="6.5"/><line x1="8" y1="5" x2="8" y2="9"/><path d="M5 13.5l3 3 3-3"/></svg>';
  const dial = el('div', { class: m.whole ? 'fdial fixed' : 'fdial' }, wheelIco, freq, el('span', { class: 'fsteps' }, up, down));
  dial.addEventListener('wheel', e => { e.preventDefault(); setActive(c.id); wheelTune(c, e); }, { passive: false });
  freq.onchange = () => {
    const v = parseFloat(freq.value.replace(/\s/g, '').replace(',', '.'));
    if (!isNaN(v)) send({ t: 'retune', ch: c.id, freq: audioOnly() ? v : v * 1000 });
  };
  freq.onkeydown = e => { if (e.key === 'Enter') freq.blur(); };
  const listen = el('button', { class: 'icon-btn small listen', type: 'button', title: 'Écouter l\'audio reçu par ce canal', text: 'Écouter' });
  listen.onclick = () => toggleListen(c);
  const pause = el('button', { class: 'icon-btn small', type: 'button', title: 'Pause' });
  pause.onclick = () => send({ t: 'pause', ch: c.id, paused: !c.paused });
  const clear = el('button', { class: 'icon-btn small', type: 'button', title: 'Effacer le texte', text: 'Effacer' });
  clear.onclick = () => {
    c.out.replaceChildren(); c.cur = null; c.table = null; c.pic = null;
    if (c.mapData) {
      c.mapData = null; c.mapLayer?.clearLayers();
      if (c.mk) { c.mk.planes.clear(); c.mk.heard.clear();
        for (const { mk, g } of c.mk.gs.values()) mk.setStyle({ radius: 6, color: '#6E8193', weight: 1.5, fillColor: '#6E8193', fillOpacity: 0.15 })
          .setTooltipContent(`Station HFDL ${g.id} : ${g.name} (non entendue)<br>${g.freqs.join(', ')} kHz`); }
      if (c.mapBtn) c.mapBtn.textContent = 'Carte';
    }
  };
  const isImg = m.kind === 'img', isIdent = m.kind === 'ident';
  if (isImg) clear.title = 'Effacer les images';
  if (isIdent) {                                   // « Relancer » : nouvelle écoute au même endroit
    clear.textContent = 'Relancer'; clear.title = 'Écouter et identifier de nouveau';
    clear.onclick = () => { c.out.replaceChildren(); send({ t: 'retune', ch: c.id, params: {} }); };
  }
  const save = el('button', { class: 'icon-btn small', type: 'button', text: 'Enregistrer',
    title: isImg ? 'Télécharger la dernière image (PNG)' : 'Enregistrer le texte' });
  save.onclick = () => isImg ? saveImage(c) : saveText(c);
  if (isIdent) save.hidden = true;
  // images : recalage manuel (fax), taille réelle / ajustée, plein écran
  const isFax = m.id === 'wefax';
  const recal = isFax ? el('button', { class: 'icon-btn small', type: 'button', text: 'Recaler',
    title: 'Image décalée ? Cliquez ensuite sur le vrai bord gauche de la carte (ou au milieu de sa marge blanche)' }) : null;
  if (recal) recal.onclick = () => {
    c.aligning = !c.aligning; c.card.classList.toggle('aligning', c.aligning);
    if (c.aligning) toast('Cliquez sur le vrai bord gauche de la carte, dans l\'image.');
  };
  const zoom = isImg && m.id !== 'hell' ? el('button', { class: 'icon-btn small', type: 'button', text: '1:1',
    title: 'Afficher l\'image en taille réelle (défilement horizontal) / l\'ajuster à la fenêtre' }) : null;
  if (zoom) zoom.onclick = () => {
    const on = c.out.classList.toggle('native');
    zoom.textContent = on ? 'Ajuster' : '1:1';
  };
  const big = isImg ? el('button', { class: 'icon-btn small', type: 'button', text: '⛶', title: 'Plein écran (Échap pour revenir)' }) : null;
  if (big) big.onclick = () => document.fullscreenElement ? document.exitFullscreen() : c.card.requestFullscreen?.();
  const hasMap = m.id === 'hfdl';
  const mapBtn = hasMap ? el('button', { class: 'icon-btn small', type: 'button', title: 'Carte des avions et des stations au sol', text: 'Carte' }) : null;
  if (mapBtn) mapBtn.onclick = () => toggleMap(c);
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
  const out = el('div', { class: isImg ? 'out img' : isIdent ? 'out ident' : 'out', tabindex: '0' });
  const spec = el('canvas', { class: 'spec', title: 'Spectre du canal : cliquez sur le signal, ou molette pour accorder (Maj : 1 Hz, Alt : 100 Hz)' });
  const specWrap = el('div', { class: 'spec-wrap' }, spec, el('div', { class: 'spec-read' }));
  const card = el('article', { class: isFax ? 'chan imgcard' : 'chan', style: `--c:${c.color}` },
    el('header', {}, el('span', { class: 'mname', text: m.label }), dial, el('span', { class: 'unit' }),
      el('span', { class: 'state' }), el('div', { class: 'tools' }, listen, pause, mapBtn, recal, zoom, big, clear, save, close)),
    el('div', { class: 'sub' }, params, meters), specWrap, hasMap ? el('div', { class: 'map', hidden: true }) : null, out);
  Object.assign(c, { card, out, freqIn: freq, meters, pauseBtn: pause, listenBtn: listen, stateEl: card.querySelector('.state'),
    mapEl: card.querySelector('.map'), mapBtn,
    spec, specRead: specWrap.querySelector('.spec-read') });
  card.addEventListener('mousedown', () => setActive(c.id));
  out.addEventListener('click', e => {
    const p = c.pic;
    if (!c.aligning || !p || !p.canvas || e.target !== p.canvas) return;
    const r = p.canvas.getBoundingClientRect();
    const x = Math.round((e.clientX - r.left) / r.width * p.w);
    send({ t: 'imgshift', ch: c.id, x });
    c.aligning = false; card.classList.remove('aligning');
  });
  spec.addEventListener('click', e => {
    if (!c.specRange) return;
    const r = spec.getBoundingClientRect(), [a, b] = c.specRange;
    let f = a + (e.clientX - r.left) / r.width * (b - a);
    f = snapSpec(c, f);
    tuneChan(c, Math.round(f));
  });
  spec.addEventListener('wheel', e => { e.preventDefault(); setActive(c.id); wheelTune(c, e); }, { passive: false });
  $('#chans').prepend(card);
  setActive(c.id);
}

// ------------------------------------------------------------------ écoute d'un canal
const AU = { ctx: null, gain: null, t: 0 };
function toggleListen(c) {
  S.listen = S.listen === c.id ? null : c.id;
  send({ t: 'listen', ch: S.listen });
  if (S.listen) {
    if (!AU.ctx) {
      AU.ctx = new AudioContext();
      AU.gain = AU.ctx.createGain(); AU.gain.gain.value = 1; AU.gain.connect(AU.ctx.destination);
    }
    AU.ctx.resume(); AU.t = 0; AU.peak = 0.05;
  }
  for (const x of S.chans.values()) refreshCard(x);
}
function onListenFrame(buf) {
  if (!AU.ctx || !S.listen) return;
  const dv = new DataView(buf), fs = dv.getUint32(9, true);
  const i16 = new Int16Array(buf.slice(13));
  if (!i16.length) return;
  const f32 = new Float32Array(i16.length);
  let pk = 0;
  for (let i = 0; i < i16.length; i++) { f32[i] = i16[i] / 32768; pk = Math.max(pk, Math.abs(f32[i])); }
  // gain automatique doux : l'audio des serveurs arrive souvent très bas
  AU.peak = Math.max(pk, AU.peak * 0.995);
  AU.gain.gain.setTargetAtTime(Math.min(40, 0.5 / (AU.peak + 1e-4)), AU.ctx.currentTime, 0.3);
  const b = AU.ctx.createBuffer(1, f32.length, fs);
  b.copyToChannel(f32, 0);
  const src = AU.ctx.createBufferSource(); src.buffer = b; src.connect(AU.gain);
  const now = AU.ctx.currentTime;
  if (AU.t < now + 0.05 || AU.t > now + 1.0) AU.t = now + 0.25;    // tampon de 250 ms
  src.start(AU.t); AU.t += b.duration;
}

// ------------------------------------------------------------------ accord fin (molette, mini-spectre)
function setActive(id) {
  S.active = id;
  for (const c of S.chans.values()) c.card.classList.toggle('active', c.id === id);
  placeMarkers();
}
function stepOf(e) { return e.shiftKey ? 1 : e.altKey ? 100 : 10; }
function wheelTune(c, e) {
  if (S.byId[c.mode]?.whole) return;
  const dir = (e.deltaY || e.deltaX) < 0 ? 1 : -1;
  tuneChan(c, Math.round((c.pendingFreq ?? c.freq) + dir * stepOf(e)));
}
function tuneChan(c, f) {
  // affichage immédiat, envoi au moteur au plus toutes les 80 ms
  c.pendingFreq = f; c.freq = f; c.lastTune = Date.now(); refreshCard(c); placeMarkers();
  if (c.tuneTimer) return;
  c.tuneTimer = setTimeout(() => {
    c.tuneTimer = null;
    send({ t: 'retune', ch: c.id, freq: c.pendingFreq });
  }, 80);
}
function snapSpec(c, f) {
  // dans le mini-spectre, on s'accroche au pic le plus proche (±15 Hz) s'il est net
  const v = c.specBins, [a, b] = c.specRange; if (!v) return f;
  const n = v.length, i0 = Math.round((f - a) / (b - a) * n), w = Math.max(1, Math.round(15 / ((b - a) / n)));
  let bi = -1, bv = -1;
  for (let i = Math.max(0, i0 - w); i <= Math.min(n - 1, i0 + w); i++) if (v[i] > bv) { bv = v[i]; bi = i; }
  const sorted = Array.from(v).sort((p, q) => p - q), floor = sorted[Math.floor(n * .5)];
  if (bi < 0 || bv < floor + 12) return f;
  return a + (bi + .5) / n * (b - a);
}
function onSpecFrame(buf) {
  const dv = new DataView(buf);
  const id = new TextDecoder().decode(new Uint8Array(buf, 1, 8)).replace(/\0+$/, '').trim();
  const c = S.chans.get(id); if (!c || !c.spec) return;
  const f0 = dv.getFloat64(9, true), f1 = dv.getFloat64(17, true), mk = dv.getFloat64(25, true);
  const v = new Uint8Array(buf, 33);
  c.specRange = [f0, f1]; c.specBins = v; c.lockFreq = mk;
  drawSpec(c);
}
function drawSpec(c) {
  const cv = c.spec, dpr = devicePixelRatio || 1, r = cv.getBoundingClientRect();
  const w = Math.max(50, Math.round(r.width * dpr)), h = Math.max(20, Math.round(r.height * dpr));
  if (cv.width !== w || cv.height !== h) { cv.width = w; cv.height = h; }
  const ctx = cv.getContext('2d'), v = c.specBins, [a, b] = c.specRange, n = v.length;
  ctx.fillStyle = '#101A23'; ctx.fillRect(0, 0, w, h);
  const sorted = Array.from(v).sort((p, q) => p - q);
  const lo = sorted[Math.floor(n * .2)] - 4, hi = Math.max(sorted[n - 1], lo + 30);
  const Y = x => h - 2 - Math.max(0, Math.min(1, (x - lo) / (hi - lo))) * (h - 6 * dpr);
  // bande du décodeur
  const m = S.byId[c.mode], bw = m.whole ? 0 : c.bw || 100;
  const X = f => (f - a) / (b - a) * w;
  if (bw) { ctx.fillStyle = c.color + '30'; ctx.fillRect(X(c.freq - bw / 2), 0, X(c.freq + bw / 2) - X(c.freq - bw / 2), h); }
  ctx.beginPath(); ctx.moveTo(0, h);
  for (let i = 0; i < n; i++) ctx.lineTo(i / (n - 1) * w, Y(v[i]));
  ctx.lineTo(w, h); ctx.closePath(); ctx.fillStyle = '#C9A24A40'; ctx.fill();
  ctx.beginPath();
  for (let i = 0; i < n; i++) { const x = i / (n - 1) * w, y = Y(v[i]); i ? ctx.lineTo(x, y) : ctx.moveTo(x, y); }
  ctx.strokeStyle = '#E3C77E'; ctx.lineWidth = dpr; ctx.stroke();
  if (!m.whole) {
    ctx.strokeStyle = c.color; ctx.lineWidth = 2 * dpr;           // fréquence demandée
    ctx.beginPath(); ctx.moveTo(X(c.freq), 0); ctx.lineTo(X(c.freq), h); ctx.stroke();
    if (c.lockFreq && Math.abs(c.lockFreq - c.freq) > 0.5) {         // où le décodeur s'est calé
      ctx.setLineDash([3 * dpr, 3 * dpr]); ctx.strokeStyle = '#FFFFFF';
      ctx.beginPath(); ctx.moveTo(X(c.lockFreq), 0); ctx.lineTo(X(c.lockFreq), h); ctx.stroke(); ctx.setLineDash([]);
    }
  }
  const span = b - a;
  c.specRead.textContent = m.whole ? '' : `${Math.round(span)} Hz · ${(span / (r.width || 1)).toFixed(1)} Hz/pixel`;
}
function refreshCard(c) {
  if (document.activeElement !== c.freqIn) c.freqIn.value = audioOnly() ? Math.round(c.freq) : fmtKHz(c.freq);
  c.card.querySelector('.unit').textContent = audioOnly() ? 'Hz audio' : 'kHz';
  c.stateEl.textContent = c.paused ? 'en pause' : c.state + (c.error ? ` : ${c.error}` : '');
  c.stateEl.classList.toggle('bad', !c.paused && (c.state === "pas d'audio reçu" || c.state === 'erreur'));
  if (c.listenBtn) c.listenBtn.classList.toggle('on', S.listen === c.id);
  c.pauseBtn.textContent = c.paused ? 'Reprendre' : 'Pause';
  c.card.classList.toggle('paused', !!c.paused);
  for (const [key, [sel, p]] of Object.entries(c.paramSel || {})) {
    const i = p.opts.findIndex(o => o[0] === c.params[key]);
    if (i >= 0) sel.value = i;
  }
}
function updateStat(c, s) {
  const parts = [];
  if (s.dbfs != null && isFinite(s.dbfs)) parts.push(`audio <b>${Math.round(s.dbfs)} dBFS</b>`);
  if (s.snr != null && isFinite(s.snr)) parts.push(`S/B <b>${Math.round(s.snr)} dB</b>`);
  if (s.quality != null) parts.push(`qualité <b>${Math.round(s.quality * 100)} %</b>`);
  if (s.wpm != null) parts.push(`<b>${Math.round(s.wpm)}</b> mpm`);
  if (s.sync != null) parts.push(s.sync ? 'synchro <b>oui</b>' : 'synchro non');
  if (s.last != null) parts.push(`<b>${s.last}</b> décodés`);
  if (s.ready === false) parts.push('<b>décodeur absent</b>');
  if (s.info) parts.push(esc(s.info));
  if (s.ident === 'écoute') parts.push(`écoute <b>${Math.round((s.progress || 0) * 100)} %</b>`);
  else if (s.ident === 'analyse') parts.push('<b>analyse…</b>');
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
function writeTr(c, text) {
  // traduction (bulletins météo) : sous la ligne qu'elle explique
  const out = c.out;
  const atBottom = out.scrollHeight - out.scrollTop - out.clientHeight < 40;
  const d = el('div', { class: 'tr', text: '→ ' + text });
  if (c.cur && c.cur.textContent === '' && c.cur.parentNode === out) out.insertBefore(d, c.cur); else out.append(d);
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
  if (m.pos || m.gs || m.heard) mapUpdate(c, m);
  c.table.append(tr);
  while (c.table.rows.length > 1500) c.table.deleteRow(0);
  if (atBottom) out.scrollTop = out.scrollHeight;
}
// ------------------------------------------------------------------ carte (HFDL)
const PLANE_SVG = '<svg viewBox="0 0 24 24" width="22" height="22"><path d="M12 2c.8 0 1.4.7 1.4 1.6v5.6l7.6 4.6v2l-7.6-2.4v4.4l2.2 1.7v1.5L12 20l-3.6 1v-1.5l2.2-1.7v-4.4L3 15.8v-2l7.6-4.6V3.6C10.6 2.7 11.2 2 12 2z"/></svg>';
function bearing(a, b) {
  const r = Math.PI / 180, y = Math.sin((b.lon - a.lon) * r) * Math.cos(b.lat * r);
  const x = Math.cos(a.lat * r) * Math.sin(b.lat * r) - Math.sin(a.lat * r) * Math.cos(b.lat * r) * Math.cos((b.lon - a.lon) * r);
  return (Math.atan2(y, x) / r + 360) % 360;
}
function mapUpdate(c, m) {
  const d = (c.mapData ||= { planes: new Map(), gs: new Map(), heard: new Map() });
  for (const g of m.gs || []) d.gs.set(g.id, { ...g, seen: m.utc });
  for (const p of m.pos || []) {
    // identité stable : code ICAO s'il est connu, sinon indicatif du vol ; le numéro de liaison
    // (« avion 42 ») est réattribué par les stations à d'autres avions, il ne sert qu'en dernier recours
    const icao = (p.ac || '').match(/ICAO ([0-9A-F]{6})/);
    const key = icao ? icao[1] : (p.flight || p.ac || `${p.lat},${p.lon}`);
    const pl = d.planes.get(key) || { key, track: [] };
    const last = pl.track[pl.track.length - 1];
    if (!last || last.lat !== p.lat || last.lon !== p.lon) pl.track.push({ lat: p.lat, lon: p.lon });
    if (pl.track.length > 50) pl.track.shift();
    Object.assign(pl, p, { seen: m.utc });
    d.planes.set(key, pl);
  }
  const placed = new Set([...d.planes.values()].map(p => p.ac).filter(Boolean));
  for (const h of m.heard || []) {
    if (placed.has(h.ac)) { d.heard.delete(h.ac); continue; }
    d.heard.delete(h.ac);                                  // réinséré à la fin : les plus récents en dernier
    d.heard.set(h.ac, { ...h, seen: m.utc });
    while (d.heard.size > 60) d.heard.delete(d.heard.keys().next().value);
  }
  for (const a of placed) d.heard.delete(a);
  if (c.map && !c.mapPending) {                            // un seul dessin par image, même en rafale
    c.mapPending = true;
    requestAnimationFrame(() => { c.mapPending = false; mapDraw(c); });
  }
  if (c.mapBtn) {
    const n = d.planes.size, u = d.heard.size;
    c.mapBtn.textContent = n || u ? `Carte (${n}${u ? ' + ' + u : ''})` : 'Carte';
    c.mapBtn.title = `${n} avion(s) positionné(s), ${u} entendu(s) sans position`;
  }
}
function toggleMap(c) {
  const div = c.mapEl;
  div.hidden = !div.hidden;
  c.card.classList.toggle('wide', !div.hidden);       // carte : la carte du canal prend toute la largeur
  if (div.hidden) return;
  if (!window.L) { div.textContent = 'Bibliothèque de carte absente (web/vendor/leaflet).'; return; }
  if (!c.map) {
    c.map = L.map(div, { worldCopyJump: true, minZoom: 1 }).setView([40, 0], 2);
    c.map.attributionControl.setPrefix(false);
    c.map.attributionControl.setPrefix(false);
    // fond hors ligne (pays, embarqué) : la carte reste lisible sans Internet
    c.map.createPane('world').style.zIndex = 150;
    fetch('vendor/world/countries-110m.json').then(r => r.json()).then(g => {
      L.geoJSON(g, { pane: 'world', interactive: false,
        style: { color: '#4A6276', weight: 0.8, fillColor: '#1E2C39', fillOpacity: 1 } }).addTo(c.map);
    }).catch(() => {});
    // fond détaillé OpenStreetMap (sans clé), assombri ; s'il ne charge pas, le fond hors ligne reste visible
    L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
      maxZoom: 12, className: 'osm-dark',
      attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">contributeurs OpenStreetMap</a>',
    }).addTo(c.map);
    c.mk = { planes: new Map(), heard: new Map(), gs: new Map() };
    c.mapLayer = L.layerGroup().addTo(c.map);
    // les 16 stations au sol, toujours affichées : grises tant qu'on ne les a pas entendues
    for (const g of S.catalog.hfdl_stations || []) {
      const mk = L.circleMarker([g.lat, g.lon], { radius: 6, color: '#6E8193', weight: 1.5, fillColor: '#6E8193', fillOpacity: 0.15 })
        .bindTooltip(`Station HFDL ${g.id} : ${g.name} (non entendue)<br>${g.freqs.join(', ')} kHz`, { direction: 'top' })
        .addTo(c.map);
      c.mk.gs.set(g.id, { mk, g });
    }
    // suivi automatique tant qu'on ne déplace pas la carte soi-même
    c.mapAuto = true;
    const manual = () => { c.mapAuto = false; };
    div.addEventListener('mousedown', manual);
    div.addEventListener('wheel', manual, { passive: true });
    div.addEventListener('touchstart', manual, { passive: true });
    const Fit = L.Control.extend({ options: { position: 'topleft' }, onAdd: () => {
      const b = L.DomUtil.create('a', 'map-fit');
      b.href = '#'; b.title = 'Tout voir'; b.textContent = '⤢';
      L.DomEvent.on(b, 'mousedown wheel', L.DomEvent.stopPropagation);
      L.DomEvent.on(b, 'click', e => { L.DomEvent.preventDefault(e); mapFit(c, true); });
      const box = L.DomUtil.create('div', 'leaflet-bar'); box.append(b); return box;
    } });
    c.map.addControl(new Fit());
    const Legend = L.Control.extend({ options: { position: 'bottomleft' }, onAdd: () => {
      const e = L.DomUtil.create('div', 'map-legend');
      e.innerHTML = '<span class="lg-plane">✈</span> position reçue · <span class="lg-heard">●</span> entendu sans position '
        + '(autour de sa station) · <span class="lg-gs">●</span> station entendue · <span class="lg-gs0">○</span> station muette';
      return e;
    } });
    c.map.addControl(new Legend());
  }
  setTimeout(() => { c.map.invalidateSize(); mapDraw(c); mapFit(c, true); }, 50);
}
function mapFit(c, force) {
  if (!c.map || !c.mapData || (!force && !c.mapAuto)) return;
  const d = c.mapData, pts = [];
  for (const p of d.planes.values()) pts.push([p.lat, p.lon]);
  for (const g of d.gs.values()) pts.push([g.lat, g.lon]);
  if (!pts.length) return;
  const b = L.latLngBounds(pts);
  // cadrage à l'ouverture et sur le bouton ⤢ ; ensuite la carte ne bouge plus d'elle-même,
  // sauf si le cadre est encore vide (premières positions reçues)
  if (!force && (c.mapFitN || 0) > 0) return;
  c.mapFitN = pts.length;
  c.map.fitBounds(b.pad(0.25), { maxZoom: 5, animate: false });
}
function mapDraw(c) {
  const d = c.mapData; if (!d || !c.map) return;
  // stations entendues : dorées
  for (const [id, g] of d.gs) {
    const s = c.mk.gs.get(id); if (!s) continue;
    s.mk.setStyle({ radius: 7, color: '#C9A24A', weight: 2, fillColor: '#C9A24A', fillOpacity: 0.45 });
    s.mk.setTooltipContent(`Station HFDL ${id} : ${s.g.name}<br>entendue à ${fmtUtc(g.seen)} UTC<br>${s.g.freqs.join(', ')} kHz`);
  }
  // avions positionnés : mis à jour sur place (une bulle ouverte reste ouverte)
  const nowMin = (() => { const t = new Date(); return t.getUTCHours() * 60 + t.getUTCMinutes(); })();
  for (const [key, p] of d.planes) {
    const n = p.track.length, hdg = n > 1 ? Math.round(bearing(p.track[n - 2], p.track[n - 1])) : 0;
    const age = p.seen ? (nowMin - (+p.seen.slice(0, 2) * 60 + +p.seen.slice(2, 4)) + 1440) % 1440 : 0;
    const old = age > 30;                                     // plus de 30 min sans nouvelle position : estompé
    const icon = L.divIcon({ className: 'plane-ico' + (old ? ' old' : ''), iconSize: [22, 22], iconAnchor: [11, 11],
      html: `<div style="transform:rotate(${hdg}deg)">${PLANE_SVG}</div>` });
    const sig = `${hdg}|${old}`;
    const html = `<b>${p.flight || '?'}</b>${p.ac ? ' · ' + p.ac : ''}<br>${p.lat.toFixed(3)}°, ${p.lon.toFixed(3)}°<br>`
      + `position de ${p.time} UTC, reçue à ${fmtUtc(p.seen)} UTC` + (n > 1 ? `<br>${n} positions` : '');
    let o = c.mk.planes.get(key);
    if (!o) {
      o = { line: L.polyline([], { color: '#39FF14', weight: 2, opacity: 0.6 }).addTo(c.mapLayer),
        mk: L.marker([p.lat, p.lon], { icon }).addTo(c.mapLayer)
          .bindTooltip(p.flight || p.ac || '?', { permanent: true, direction: 'right', offset: [10, 0], className: 'plane-lbl' })
          .bindPopup(html) };
      c.mk.planes.set(key, o);
    } else {
      o.mk.setLatLng([p.lat, p.lon]).setPopupContent(html);
      if (o.sig !== sig) o.mk.setIcon(icon);
    }
    o.sig = sig;
    o.line.setLatLngs(n > 1 ? p.track.map(t => [t.lat, t.lon]) : []);
  }
  // avions entendus sans position : en orange, en couronne autour de la station avec laquelle ils parlent
  const byGs = new Map();
  for (const h of d.heard.values()) { if (!byGs.has(h.gs)) byGs.set(h.gs, []); byGs.get(h.gs).push(h); }
  const keep = new Set();
  for (const [gid, list] of byGs) {
    const st = (S.catalog.hfdl_stations || []).find(g => g.id === gid); if (!st) continue;
    list.forEach(h => {
      // place fixe pour chaque avion (dérivée de son nom) : les points ne sautent plus d'un message à l'autre
      let hsh = 0; for (const ch of h.ac) hsh = (hsh * 31 + ch.charCodeAt(0)) >>> 0;
      const slot = hsh % 48, ang = 2 * Math.PI * (slot % 16) / 16, r = 2.2 + 1.1 * Math.floor(slot / 16);
      const ll = [st.lat + r * Math.sin(ang), st.lon + r * Math.cos(ang) / Math.max(0.2, Math.cos(st.lat * Math.PI / 180))];
      const tip = `${h.ac} : position inconnue<br>en liaison avec ${st.name}, à ${fmtUtc(h.seen)} UTC`;
      let o = c.mk.heard.get(h.ac);
      if (!o) {
        o = L.circleMarker(ll, { radius: 4, color: '#FF9F1C', weight: 1, fillColor: '#FF9F1C', fillOpacity: 0.8 })
          .bindTooltip(tip, { direction: 'top' }).addTo(c.mapLayer);
        c.mk.heard.set(h.ac, o);
      } else { o.setLatLng(ll).setTooltipContent(tip); }
      keep.add(h.ac);
    });
  }
  for (const [a, o] of c.mk.heard) if (!keep.has(a)) { o.remove(); c.mk.heard.delete(a); }
  mapFit(c, false);
}
function fmtUtc(u) { return u ? `${u.slice(0, 2)}:${u.slice(2, 4)}` : '?'; }

// ------------------------------------------------------------------ identification
const WHY = { empreinte: 'empreinte', largeur: 'largeur', modulation: 'modulation', 'fréquence': 'fréquence', ACF: 'ACF' };
function fmtMeasure(m) {
  const n = v => v.toLocaleString('fr-FR', { maximumFractionDigits: v < 20 ? 2 : 0 });
  const p = [`${n(m.f1)}–${n(m.f2)} Hz audio`, `largeur <b>${n(m.bw)} Hz</b>`];
  if (m.ntones >= 2 && m.spacing) p.push(`<b>${m.ntones}</b> tonalités à <b>${n(m.spacing)} Hz</b>`);
  if (m.baud) p.push(`<b>${n(m.baud)}</b> bauds`);
  if (m.psk) p.push(`PSK d'ordre <b>${m.psk}</b>`);
  if (m.acf) p.push(`ACF <b>${n(m.acf)} ms</b>`);
  if (m.envvar < 0.15) p.push('enveloppe constante');
  if (m.gaps > 0.35) p.push('manipulation tout ou rien');
  p.push(`<b>${Math.round(m.snr)} dB</b> au-dessus du bruit`);
  return p.join(' · ');
}
function openFound(c, o) {
  if (!o) return;
  addChannel(o.mode, o.freq, o.params, c.id);       // le canal d'identification cède sa place
}
function renderIdent(c, m) {
  const out = c.out; out.replaceChildren();
  if (m.error) { out.append(el('p', { class: 'id-none', text: m.error })); return; }
  if (m.measure) { const p = el('p', { class: 'id-meas' }); p.innerHTML = fmtMeasure(m.measure); out.append(p); }
  const auto = c.params.auto !== false;
  if (m.confirmed) {
    const k = m.confirmed;
    const box = el('div', { class: 'id-ok' },
      el('div', { class: 'id-head' }, el('span', { class: 'id-badge', text: 'Confirmé par décodage' }),
        el('b', { text: k.label }), el('span', { class: 'id-par', text: paramText(k.mode, k.params) })),
      el('div', { class: 'id-text', text: k.text }));
    if (!auto) box.append(el('button', { class: 'btn', type: 'button', text: `Ouvrir un canal ${k.label}`, onclick: () => openFound(c, m.open) }));
    out.append(box);
  } else if (m.phase === 'candidats') {
    out.append(el('p', { class: 'id-wait', text: m.candidates.some(x => x.decodable) ? 'Vérification par décodage des candidats…' : '' }));
  } else if (m.candidates.length) {
    out.append(el('p', { class: 'id-none', text: m.candidates.some(x => x.decodable)
      ? 'Aucun décodeur n\'a confirmé : voici les signaux les plus ressemblants.'
      : 'Signal que Orsat-Decoder ne décode pas : voici les plus ressemblants.' }));
  }
  if (!m.candidates.length) return;
  const top = Math.max(...m.candidates.map(x => x.score)), low = Math.min(...m.candidates.map(x => x.score), top - 3);
  const ol = el('ol', { class: 'id-list' });
  for (const k of m.candidates) {
    const pct = Math.max(4, Math.round((k.score - low) / (top - low || 1) * 100));
    const li = el('li', {},
      el('div', { class: 'id-row' },
        el('span', { class: 'id-title', text: k.title }),
        k.variant ? el('span', { class: 'id-var', text: k.variant.label }) : null,
        el('span', { class: 'id-bar', title: `note ${k.score}` }, el('i', { style: `width:${pct}%` })),
        k.open ? el('button', { class: 'icon-btn small', type: 'button', text: 'Ouvrir', title: 'Ouvrir un canal dans ce mode',
          onclick: () => openFound(c, k.open) }) : null),
      el('div', { class: 'id-why', text: k.why.length ? 'Concorde : ' + k.why.map(w => WHY[w] || w).join(', ') : 'ressemblance faible' }));
    ol.append(li);
  }
  out.append(ol);
}
function paramText(mode, params) {
  const md = S.byId[mode]; if (!md || !params) return '';
  return (md.params || []).filter(p => p.key in params && p.key !== 'reverse')
    .map(p => (p.opts.find(o => o[0] === params[p.key]) || [, params[p.key]])[1]).join(' · ')
    + (params.reverse ? ' · inversé' : '');
}
// ------------------------------------------------------------------ images (fax, SSTV, Hell)
const b64bytes = s => { const b = atob(s), u = new Uint8Array(b.length); for (let i = 0; i < b.length; i++) u[i] = b.charCodeAt(i); return u; };
function onImage(c, m) {
  const out = c.out;
  const atBottom = out.scrollHeight - out.scrollTop - out.clientHeight < 40;
  if (m.op === 'new') {
    const fig = el('figure', { class: m.tape ? 'pic tape' : 'pic' });
    const cap = el('figcaption', { text: `${m.title || ''}  ·  ${new Date().toLocaleTimeString('fr-FR', { hour: '2-digit', minute: '2-digit' })}` });
    if (!m.tape) fig.append(cap);
    c.pic = { ...m, fig, cap, canvas: null, ctx: null, rows: 0, x: 0, cols: 0 };
    if (!m.tape) newPicCanvas(c.pic, m.h || 400);
    out.append(fig);
    while (out.childElementCount > 12) out.firstChild.remove();
  } else if (m.op === 'saved') {
    if (c.pic && c.pic.cap) c.pic.cap.textContent += `  ·  enregistrée : ${m.name}`;
    else toast(`Image enregistrée : ${m.name}`);
    return;
  } else if (m.op === 'end') {
    if (c.pic && !c.pic.tape) c.pic.done = true;
    return;
  }
  const p = c.pic; if (!p) return;
  if (m.op === 'rows') {
    const rgb = p.fmt === 'rgb', w = p.w, src = b64bytes(m.data);
    const need = m.y + m.n;
    if (need > p.canvas.height) newPicCanvas(p, Math.max(need, Math.ceil(p.canvas.height * 1.5)));
    const img = p.ctx.createImageData(w, m.n), d = img.data;
    for (let i = 0, j = 0, k = 0; i < w * m.n; i++, k += 4) {
      if (rgb) { d[k] = src[j++]; d[k + 1] = src[j++]; d[k + 2] = src[j++]; }
      else { d[k] = d[k + 1] = d[k + 2] = src[j++]; }
      d[k + 3] = 255;
    }
    p.ctx.putImageData(img, 0, m.y);
    if (m.total != null) {                           // image recalée : renvoyée en entier
      p.rows = m.total;
      if (p.canvas.height > m.total) { p.ctx.fillStyle = '#000'; p.ctx.fillRect(0, m.total, w, p.canvas.height - m.total); }
    } else p.rows = Math.max(p.rows, need);
  } else if (m.op === 'cols') {
    const h = p.h, src = b64bytes(m.data), n = m.n;
    let i = 0;
    while (i < n) {
      if (!p.canvas || p.x >= p.canvas.width) {     // nouvelle ligne de bande, comme une ligne de texte (pixels 1:1)
        const cv = el('canvas', { width: Math.max(300, c.out.clientWidth - 16), height: h });
        p.canvas = cv; p.ctx = cv.getContext('2d'); p.x = 0;
        p.ctx.fillStyle = '#fff'; p.ctx.fillRect(0, 0, cv.width, h);
        p.fig.append(cv);
        while (p.fig.childElementCount > 60) p.fig.firstChild.remove();
      }
      const k = Math.min(n - i, p.canvas.width - p.x);
      const img = p.ctx.createImageData(k, h), d = img.data;
      for (let x = 0; x < k; x++) for (let y = 0; y < h; y++) {          // colonnes : haut → bas
        const v = src[(i + x) * h + y], q = (y * k + x) * 4;
        d[q] = d[q + 1] = d[q + 2] = v; d[q + 3] = 255;
      }
      p.ctx.putImageData(img, p.x, 0);
      p.x += k; i += k;
    }
  }
  if (atBottom) out.scrollTop = out.scrollHeight;
}
function newPicCanvas(p, h) {
  const cv = el('canvas', { width: p.w, height: h });
  const ctx = cv.getContext('2d');
  ctx.fillStyle = '#000'; ctx.fillRect(0, 0, p.w, h);
  if (p.canvas) { ctx.drawImage(p.canvas, 0, 0); p.canvas.replaceWith(cv); } else p.fig.append(cv);
  p.canvas = cv; p.ctx = ctx;
}
function saveImage(c) {
  const p = c.pic; if (!p || !p.canvas) { toast('Aucune image pour l\'instant.'); return; }
  let cv = p.canvas;
  if (p.tape) {                                      // toutes les lignes de bande, l'une sous l'autre
    const all = [...p.fig.querySelectorAll('canvas')];
    cv = el('canvas', { width: Math.max(...all.map(k => k.width)), height: all.length * p.h });
    const x = cv.getContext('2d'); all.forEach((k, i) => x.drawImage(k, 0, i * p.h));
  } else if (p.rows && p.rows < cv.height) {          // sans le bas encore vide
    const k = el('canvas', { width: p.w, height: p.rows });
    k.getContext('2d').drawImage(cv, 0, 0); cv = k;
  }
  const m = S.byId[c.mode];
  cv.toBlob(b => {
    const a = el('a', { href: URL.createObjectURL(b), download: `orsat-${m.id}-${Math.round(c.freq / 1000)}kHz-${new Date().toISOString().slice(0, 16).replace(/[:T]/g, '-')}.png` });
    document.body.append(a); a.click(); a.remove();
  }, 'image/png');
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
const audioOnly = () => !!(S.server && S.server.shared && S.server.nodial);
function fmtF(f) { return audioOnly() ? `${Math.round(f)} Hz` : `${fmtKHz(f)} kHz`; }
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
    ctx.fillStyle = '#8394A5'; ctx.fillText(audioOnly() ? String(Math.round(f)) : fmtKHz(f), x, scaleH / 2 - 2 * dpr);
  }
  ctx.strokeStyle = '#2C3C4C'; ctx.beginPath(); ctx.moveTo(0, scaleH - .5); ctx.lineTo(w, scaleH - .5); ctx.stroke();
}
function onWfFrame(buf) {
  if (!S.view || !WF.w) return;
  const dv = new DataView(buf);
  if (dv.getUint8(0) !== 1) return;
  const lf0 = dv.getFloat64(1, true), lf1 = dv.getFloat64(9, true);
  if (lf1 < S.view[0] || lf0 > S.view[1]) return;
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
  if (WF.warm > 0) { WF.warm--; return; }     // quelques lignes pour estimer bruit et crête avant d'afficher
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
  renderRibbon();
  const box = $('#markers'); box.replaceChildren();
  if (!S.view || !WF.c) return;
  for (const c of S.chans.values()) {
    const m = S.byId[c.mode]; if (!m) continue;
    let a, b;
    if (m.demod === 'FM') { a = c.freq - 6000; b = c.freq + 6000; }
    else if (m.demod === 'AM') { a = c.freq - 5000; b = c.freq + 5000; }
    else if (m.whole) { a = c.freq + 200; b = c.freq + 3000; }
    else if (m.kind === 'ident') { a = c.freq - 50; b = c.freq + 50; }   // repère étroit : le waterfall reste cliquable
    else { a = c.freq - c.bw / 2; b = c.freq + c.bw / 2; }
    const xa = xOfFreq(a), xb = xOfFreq(b);
    if (xb < 0 || xa > WF.c.getBoundingClientRect().width) continue;
    const width = Math.max(4, xb - xa);
    const mk = el('div', { class: 'marker', style: `left:${xa}px;width:${width}px`, title: `${m.label} ${fmtF(c.freq)} : glisser pour réaccorder` },
      el('div', { class: 'band', style: `background:${c.color}` }),
      el('div', { class: 'edge', style: `left:0;border-color:${c.color}` }),
      el('div', { class: 'edge', style: `right:0;border-color:${c.color}` }),
      el('div', { class: 'tag', style: `background:${c.color}`, text: m.label }));
    mk.addEventListener('mousedown', e => { setActive(c.id); startDrag(e, c, mk); });
    if (c.id === S.active) mk.classList.add('active');
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
  const lo = s.basefreq, hi = s.basefreq + s.total_bandwidth, minSpan = s.shared ? 400 : 3000;
  let span = Math.max(minSpan, Math.min(hi - lo, f1 - f0));
  f0 = Math.max(lo, Math.min(hi - span, f0)); f1 = f0 + span;
  S.view = [f0, f1];
  WF.ctx.fillStyle = '#0D151D'; WF.ctx.fillRect(0, WF.scaleH, WF.w, WF.h - WF.scaleH);
  WF.floor = WF.peak = null; WF.warm = 4;
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
    if (e.target.closest('.marker, .rb, .wf-overlay')) return;
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
    if (!S.view || e.target.closest('.marker, .rb, .wf-overlay') || !e.target.closest('#wfWrap')) return;
    const m = S.byId[S.armed ? 'ident' : S.mode]; if (!m) return;
    let f = freqAtX(e.clientX);
    const hzPerPx = (S.view[1] - S.view[0]) / WF.c.getBoundingClientRect().width;
    if (!m.whole && hzPerPx > (m.kind === 'ident' ? 150 : 20)) {
      // résolution trop faible pour viser un signal : on zoome autour du clic, le clic suivant ouvrira le canal
      const span = Math.max(2000, WF.c.getBoundingClientRect().width * 8);
      setView(f - span / 2, f + span / 2);
      toast('Zoom sur le signal : cliquez dessus pour ouvrir le canal.');
      return;
    }
    if (m.whole && S.server.shared) {
      f = S.server.basefreq;
    } else if (m.demod === 'FM' || m.demod === 'AM') {
      f = Math.round(f / 500) * 500;                // FM, AM : centre du signal
    } else if (m.carrier) {
      f = Math.round(f / 100) * 100;                // tonalités mesurées depuis la porteuse (Selcal)
    } else if (m.whole) {
      const near = S.catalog.presets.filter(p => p.mode === m.id).map(p => p.freq).find(p => f >= p - 500 && f <= p + 3500);
      f = near ?? Math.round(f - 1500);
    } else if (m.kind === 'ident') {
      f = Math.round(snapFreq(f, 300, true));       // centre du signal sous le clic
    } else {
      const fsk = m.bw_from === 'shift' || m.id === 'navtex';
      f = Math.round(snapFreq(f, fsk ? 400 : (m.bw || 200), fsk));
    }
    addChannel(m.id, f);
    setArmed(false);
  });
  wrap.addEventListener('mousemove', e => {
    if (!S.view) return;
    const r = wrap.getBoundingClientRect();
    hover.style.display = 'block'; hover.style.left = (e.clientX - r.left) + 'px';
    hover.textContent = fmtF(freqAtX(e.clientX));
  });
  wrap.addEventListener('mouseleave', () => { hover.style.display = 'none'; });
  wrap.addEventListener('wheel', e => {
    e.preventDefault();
    // molette : zoom autour du curseur ; Ctrl+molette : accorde le canal actif
    const c = S.chans.get(S.active);
    if (e.ctrlKey && c && !S.byId[c.mode]?.whole) wheelTune(c, e);
    else zoom(e.deltaY < 0 ? 0.8 : 1 / 0.8, freqAtX(e.clientX));
  }, { passive: false });
  $('#zoomIn').onclick = () => zoom(0.5);
  $('#zoomOut').onclick = () => zoom(2);
  $('#zoomAll').onclick = () => { const s = S.server; if (s.basefreq != null) setView(s.basefreq, s.basefreq + s.total_bandwidth); };
}

// ------------------------------------------------------------------ bandeau de commande
function setArmed(on) {
  S.armed = on;
  const b = $('#identBtn');
  b.classList.toggle('on', on); b.setAttribute('aria-pressed', on);
  $('#wfWrap').classList.toggle('armed', on);
  renderModes();
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
  $('#sourceSel').onchange = e => {
    const v = e.target.value;
    if (v === '+add' || v === '+manage') {
      e.target.value = S.current;
      if (v === '+add') srvAdd(); else $('#settingsBtn').click();
      return;
    }
    send({ t: 'select_source', id: v });
  };
  $('#dialIn').addEventListener('keydown', e => {
    if (e.key !== 'Enter') return;
    const v = parseFloat($('#dialIn').value.replace(/\s/g, '').replace(',', '.'));
    if (!isNaN(v)) send({ t: 'qsy', freq: v * 1000 });
    $('#dialIn').blur();
  });
  $('#srcAdd').onclick = () => {
    const type = $('#srcNewType').value;
    const base = { phantom: { url: 'http://' }, kiwi: { url: 'http://', password: '' }, owrx: { url: 'http://' }, tci: { url: 'ws://127.0.0.1:50001', trx: 0 }, audio: { device: '', rigctl: '' } }[type];
    S.sources.push({ id: Math.random().toString(36).slice(2, 8), type, name: S.types[type].split(' (')[0], ...base });
    send({ t: 'sources_set', sources: S.sources });
  };
  $('#rxSel').onchange = e => send({ t: 'select_rx', rx: e.target.value });
  $('#presetBtn').onclick = e => {
    e.stopPropagation();
    const m = $('#presetMenu');
    m.hidden = !m.hidden;
    if (!m.hidden) { renderPresets(); m.querySelector('.dir-search')?.focus(); }
  };
  document.addEventListener('click', e => { if (!e.target.closest('.menu-wrap')) $('#presetMenu').hidden = true; });
  $('#settingsBtn').onclick = () => { send({ t: 'audio_inputs' }); $('#settings').showModal(); };
  $('#identBtn').onclick = () => {
    if (!S.armed && S.chans.size >= MAX_CH) { toast(`${MAX_CH} canaux au maximum : fermez-en un pour identifier un signal.`, 'error'); return; }
    setArmed(!S.armed);
    if (S.armed) toast('Cliquez sur le signal à identifier dans le waterfall.');
  };
  addEventListener('keydown', e => { if (e.key === 'Escape' && S.armed) setArmed(false); });
  $('#setPalette').onchange = e => { S.ui.palette = e.target.value; buildLut(); savePrefs(); };
  $('#setContrast').oninput = e => { S.ui.contrast = +e.target.value; savePrefs(); };
  $('#setFloor').oninput = e => { S.ui.floor = +e.target.value; savePrefs(); };
  $('#quitBtn').onclick = () => { if (confirm('Quitter Orsat-Decoder ?')) { send({ t: 'quit' }); setTimeout(() => window.close(), 500); } };
  addEventListener('resize', placeMarkers);
}
wire(); initWaterfall(); connect();
