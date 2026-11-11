const RISK = {faible: 'faible', moyen: 'moyen', eleve: 'élevé'};
const SOURCE = {modele: 'Connaissance du modèle', cache: 'Cache marque et modèle', web: 'Recherche web'};
const POLL_MS = 4000;
const PAGE = 24;
const TRADE_PAGE = 40;

export function createOffers({$, $$, esc, euro, date, post, toast}) {
  const view = {name: null, timer: null};
  const store = {offers: [], thresholds: {panel: 1.5, alert: 2.2}, appraisal: null, ledger: null, offersRev: null, ledgerRev: null, offersError: '', ledgerError: '', shown: PAGE, tradesShown: TRADE_PAGE};
  const statusOf = offer => offer.trade ? offer.trade.status : offer.status;
  const ratioText = n => n == null ? 'n/a' : n.toFixed(2).replace('.', ',') + 'x';
  const badge = level => level ? `<span class="badge ${level}">${store.thresholds[level]}x+</span>` : '';

  async function load(path, revKey, errorKey) {
    try {
      const rev = store[revKey] === null ? '' : '?rev=' + store[revKey];
      const response = await fetch(path + rev);
      if (!response.ok) throw new Error('Serveur indisponible');
      const data = await response.json();
      store[errorKey] = '';
      if (data.unchanged) return null;
      store[revKey] = data.rev;
      return data;
    } catch (error) {
      store[errorKey] = 'Mise à jour impossible : ' + (error instanceof TypeError ? 'serveur local injoignable.' : error.message);
      return null;
    }
  }

  async function refreshOffers() {
    const before = store.offersError;
    const data = await load('/api/offers', 'offersRev', 'offersError');
    if (data) {
      store.offers = data.offers.filter(o => o.status !== 'skipped');
      store.thresholds = data.thresholds;
      store.appraisal = data.appraisal;
    }
    if (data || before !== store.offersError) renderOffers();
  }

  async function refreshLedger() {
    const before = store.ledgerError;
    const data = await load('/api/ledger', 'ledgerRev', 'ledgerError');
    if (data) store.ledger = data;
    if (data || before !== store.ledgerError) renderLedger();
  }

  function filtered() {
    const brand = $('#offers-brand').value, size = $('#offers-size').value, status = $('#offers-status').value, sort = $('#offers-sort').value;
    const minMultiple = parseFloat($('#offers-min').value.replace(',', '.')), minConfidence = parseFloat($('#offers-confidence').value.replace(',', '.'));
    const text = $('#offers-search').value.trim().toLowerCase();
    const rows = store.offers.filter(o =>
      (!brand || (o.marque || o.brand) === brand) && (!size || o.size === size) && (!status || statusOf(o) === status) &&
      (isNaN(minMultiple) || (o.multiple ?? -1) >= minMultiple) &&
      (isNaN(minConfidence) || (o.confiance ?? -1) >= minConfidence / 100) &&
      (!text || (o.title + ' ' + o.marque + ' ' + o.modele).toLowerCase().includes(text)));
    const key = {multiple: o => o.multiple ?? -1, recent: o => Date.parse(o.first_seen), price: o => -o.price, confidence: o => o.confiance ?? -1}[sort];
    return rows.sort((a, b) => key(b) - key(a));
  }

  function activeFilters() {
    const set = ['#offers-brand', '#offers-size', '#offers-status', '#offers-min', '#offers-confidence'].filter(selector => $(selector).value).length;
    return set + ($('#offers-sort').value !== 'multiple' ? 1 : 0);
  }

  function fillSelect(select, values, label) {
    const selected = select.value, key = JSON.stringify(values);
    const html = `<option value="">${label}</option>` + values.map(v => `<option value="${esc(v)}">${esc(v)}</option>`).join('');
    if (select.dataset.key !== key) {
      select.dataset.key = key;
      select.innerHTML = html;
      select.value = values.includes(selected) ? selected : '';
    }
  }

  function engineNotice() {
    const a = store.appraisal;
    if (!a) return '';
    if (!a.configured) return '<div class="notice">La variable ANTHROPIC_API_KEY n’est pas définie. Les annonces sont collectées et attendent leur évaluation : elles seront traitées dès que la clé sera disponible.</div>';
    if (a.error) return `<div class="notice error" role="alert">${esc(a.error)} Les annonces restent en file et seront réessayées.</div>`;
    return '';
  }

  function offerCard(o) {
    const photo = o.image ? `<img class="offer-photo" src="${esc(o.image)}" alt="" loading="lazy" decoding="async" referrerpolicy="no-referrer">` : '<div class="offer-photo image-placeholder">Sans photo</div>';
    const link = `<a href="${esc(o.url)}" target="_blank" rel="noopener noreferrer">${esc(o.title)}</a>`;
    const trade = o.trade;
    const tradeButtons = !trade
      ? `<button class="primary" data-trade="buy" data-id="${esc(o.id)}">Marquer acheté</button>`
      : trade.status === 'achete'
        ? `<button data-trade="sell" data-id="${esc(o.id)}">Marquer vendu</button><button data-trade="buy" data-id="${esc(o.id)}">Modifier l’achat</button>`
        : `<span class="label">Vendu · bénéfice ${euro(trade.profit)}</span><button data-trade="sell" data-id="${esc(o.id)}">Modifier la vente</button>`;
    if (o.status !== 'done') {
      const waiting = o.status === 'expired'
        ? '<p class="offer-state">Trop ancienne pour être évaluée automatiquement. Le délai se règle dans Préférences.</p>'
        : o.status === 'pending'
        ? `<p class="offer-state">En attente d’évaluation${o.attempts ? ' · ' + o.attempts + ' essai(s)' : ''}.</p>${o.error ? `<p class="help">${esc(o.error)}</p>` : ''}`
        : `<p class="offer-state error">Évaluation en échec : ${esc(o.error || 'réponse inexploitable')}</p>`;
      const retry = o.status === 'failed' || o.status === 'expired' ? `<button data-retry="${esc(o.id)}">${o.status === 'expired' ? 'Évaluer quand même' : 'Réessayer l’évaluation'}</button>` : '';
      return `<article class="offer pending">${photo}<div class="offer-body"><h3 class="offer-title">${link}</h3><p class="offer-meta">${esc([o.brand, o.size, o.condition].filter(Boolean).join(' · '))}</p><p class="offer-price">${euro(o.price)} + ${euro(o.port)} de port</p>${waiting}<div class="offer-actions">${retry}</div></div></article>`;
    }
    const meta = [o.marque || o.brand, o.modele, o.size, o.condition].filter(Boolean).map(esc).join(' · ');
    const reasons = o.raisons.map(r => `<li>${esc(r)}</li>`).join('');
    return `<article class="offer ${o.level}">${photo}<div class="offer-body">
      <div class="offer-score"><strong>${ratioText(o.multiple)}</strong><span>multiple de revente</span>${badge(o.level)}</div>
      <h3 class="offer-title">${link}</h3><p class="offer-meta">${meta}</p>
      <dl class="offer-figures">
        <div><dt>Prix et port</dt><dd>${euro(o.price)} + ${euro(o.port)}</dd></div>
        <div class="wide"><dt>Revente : basse, médiane, haute</dt><dd>${euro(o.basse)} · <b>${euro(o.mediane)}</b> · ${euro(o.haute)}</dd></div>
        <div><dt>Confiance</dt><dd>${Math.round(o.confiance * 100)} %</dd></div>
        <div><dt>Contrefaçon</dt><dd class="risk ${esc(o.risque)}">${esc(RISK[o.risque] || o.risque)}</dd></div>
      </dl>
      <ul class="offer-reasons">${reasons}</ul>
      <p class="help">${esc(SOURCE[o.source] || '')} · détectée le ${date(o.first_seen)}</p>
      <div class="offer-actions">${tradeButtons}</div></div></article>`;
  }

  function keepFocus(render) {
    const active = document.activeElement;
    const kept = active && active.closest && active.closest('#offers, #ledger-rows') ? {trade: active.dataset.trade, id: active.dataset.id, retry: active.dataset.retry} : null;
    render();
    if (!kept) return;
    const pick = selector => document.querySelector(selector);
    const target = (kept.trade && pick(`[data-trade="${kept.trade}"][data-id="${CSS.escape(kept.id)}"]`)) ||
      (kept.retry && pick(`[data-retry="${CSS.escape(kept.retry)}"]`)) || (kept.id && pick(`[data-id="${CSS.escape(kept.id)}"]`));
    if (target) target.focus();
  }

  function renderOffers() {
    keepFocus(drawOffers);
  }

  function renderLedger() {
    keepFocus(drawLedger);
  }

  function drawOffers() {
    const holder = $('#offers');
    $('#offers-notice').innerHTML = engineNotice() + (store.offersError ? `<div class="notice error" role="alert">${esc(store.offersError)} <button data-reload="offers">Réessayer</button></div>` : '');
    if (!store.appraisal) {
      holder.innerHTML = `<div class="empty"><p>${store.offersError ? 'Les offres ne sont pas disponibles pour le moment.' : 'Chargement des offres…'}</p></div>`;
      return;
    }
    const all = store.offers;
    fillSelect($('#offers-brand'), [...new Set(all.map(o => o.marque || o.brand).filter(Boolean))].sort(), 'Toutes les marques');
    fillSelect($('#offers-size'), [...new Set(all.map(o => o.size).filter(Boolean))].sort(), 'Toutes les tailles');
    const done = all.filter(o => o.status === 'done');
    $('#offers-stat-done').textContent = done.length;
    $('#offers-stat-panel').textContent = done.filter(o => o.level).length;
    $('#offers-stat-alert').textContent = done.filter(o => o.level === 'alert').length;
    $('#offers-stat-pending').textContent = all.filter(o => o.status === 'pending').length;
    $('#label-panel').textContent = store.thresholds.panel + 'x et plus';
    $('#label-alert').textContent = store.thresholds.alert + 'x et plus';
    const rows = filtered();
    $('#offers-active').textContent = activeFilters() || '';
    $('#offers-count').textContent = rows.length + ' offre' + (rows.length !== 1 ? 's' : '') + ' affichée' + (rows.length !== 1 ? 's' : '');
    if (!rows.length) {
      holder.innerHTML = all.length
        ? '<div class="empty"><h2>Aucune offre ne correspond.</h2><p>Assouplis les filtres ou le multiple minimum.</p></div>'
        : '<div class="empty"><h2>Aucune annonce évaluée pour le moment.</h2><p>Active le profil Vêtements de marques dans Recherches et lance la veille. Chaque nouvelle annonce dont la marque figure dans ta liste est évaluée par Claude.</p><div class="empty-actions"><button data-view="profiles">Ouvrir les recherches</button></div></div>';
      return;
    }
    const visible = rows.slice(0, store.shown);
    holder.innerHTML = visible.map(offerCard).join('') + (rows.length > visible.length ? `<button class="more" data-more="offers">Afficher plus (${rows.length - visible.length})</button>` : '');
  }

  function summaryTile(label, value, tone = '') {
    return `<div class="tile ${tone}"><span>${label}</span><strong>${value}</strong></div>`;
  }

  function drawLedger() {
    const data = store.ledger;
    $('#ledger-notice').innerHTML = store.ledgerError ? `<div class="notice error" role="alert">${esc(store.ledgerError)} <button data-reload="ledger">Réessayer</button></div>` : '';
    if (!data) {
      $('#ledger-rows').innerHTML = '<div class="empty"><p>Chargement du bilan…</p></div>';
      return;
    }
    const s = data.summary;
    $('#ledger-summary').innerHTML = [
      summaryTile('Dépenses', euro(s.depenses)), summaryTile('Chiffre d’affaires', euro(s.chiffre_affaires)),
      summaryTile('Bénéfice', euro(s.benefice), s.benefice < 0 ? 'loss' : ''), summaryTile('Marge moyenne', s.marge_moyenne == null ? 'n/a' : String(s.marge_moyenne).replace('.', ',') + ' %'),
      summaryTile('Articles', s.articles + (s.vendus ? ' dont ' + s.vendus + ' vendus' : ''))].join('');
    const empty = !data.trades.length;
    $('#ledger-chart-box').hidden = empty;
    $('#ledger-rows').innerHTML = empty
      ? '<div class="empty"><h2>Aucun achat enregistré.</h2><p>Marque une offre comme achetée depuis Contemporain, ou avec le bouton Acheté sur Discord. Le bilan se met à jour des deux côtés.</p><div class="empty-actions"><button data-view="contemporary">Voir les offres</button></div></div>'
      : data.trades.slice().reverse().slice(0, store.tradesShown).map(t => `<div class="trade">
          ${t.image ? `<img class="trade-photo" src="${esc(t.image)}" alt="" loading="lazy" decoding="async" referrerpolicy="no-referrer">` : '<div class="trade-photo image-placeholder">Sans photo</div>'}
          <div class="trade-info"><a href="${esc(t.url)}" target="_blank" rel="noopener noreferrer">${esc(t.title)}</a><small>${[t.brand, 'acheté le ' + date(t.bought_at), t.sold_at ? 'vendu le ' + date(t.sold_at) : '', t.platform, t.fees ? 'frais ' + euro(t.fees) : ''].filter(Boolean).map(esc).join(' · ')}</small></div>
          <span class="label ${t.status}">${t.status === 'vendu' ? 'Vendu' : 'Acheté'}</span>
          <div class="trade-figures"><span>Achat ${euro(t.buy_price)}</span><span>${t.status === 'vendu' ? 'Vente ' + euro(t.sell_price) : 'Pas encore vendu'}</span></div>
          <div class="trade-profit ${t.profit != null && t.profit < 0 ? 'loss' : ''}">${t.profit == null ? '' : euro(t.profit)}<small>${t.margin == null ? '' : String(t.margin).replace('.', ',') + ' %'}</small></div>
          <div class="trade-actions"><button data-trade="sell" data-id="${esc(t.item_id)}">${t.status === 'vendu' ? 'Modifier la vente' : 'Marquer vendu'}</button><button data-trade="buy" data-id="${esc(t.item_id)}">Modifier l’achat</button></div></div>`).join('');
    if (data.trades.length > store.tradesShown) $('#ledger-rows').insertAdjacentHTML('beforeend', `<button class="more" data-more="trades">Afficher plus (${data.trades.length - store.tradesShown})</button>`);
    if (!empty) drawChart();
  }

  function palette() {
    const css = getComputedStyle(document.documentElement);
    const read = name => css.getPropertyValue(name).trim();
    return {cost: read('--accent'), revenue: read('--green'), profit: read('--ink'), muted: read('--muted'), line: read('--line'), surface: read('--surface')};
  }

  function drawChart() {
    const canvas = $('#ledger-chart');
    const points = store.ledger?.series || [];
    if (!points.length || !canvas.clientWidth) return;
    const colors = palette(), ratio = window.devicePixelRatio || 1;
    const width = canvas.clientWidth, height = Math.round(Math.min(360, Math.max(230, width * 0.38)));
    canvas.width = Math.round(width * ratio);
    canvas.height = Math.round(height * ratio);
    const ctx = canvas.getContext('2d');
    ctx.scale(ratio, ratio);
    ctx.clearRect(0, 0, width, height);
    const left = 58, right = 14, top = 12, bottom = 28;
    const times = points.map(p => Date.parse(p.at));
    const start = times[0], end = Math.max(times[times.length - 1], Date.now());
    const span = Math.max(end - start, 86400000);
    const values = points.flatMap(p => [p.depenses, p.revenus, p.benefice]).concat(0);
    const low = Math.min(...values), high = Math.max(...values, 1);
    const pad = (high - low) * 0.08 || 1, min = low < 0 ? low - pad : 0, max = high + pad;
    const x = t => left + (t - start) / span * (width - left - right);
    const y = v => top + (1 - (v - min) / (max - min)) * (height - top - bottom);
    ctx.font = '11px -apple-system, "Segoe UI", sans-serif';
    ctx.textBaseline = 'middle';
    ctx.lineWidth = 1;
    const raw = (max - min) / 4, magnitude = 10 ** Math.floor(Math.log10(raw)), factor = raw / magnitude;
    const step = (factor <= 1 ? 1 : factor <= 2 ? 2 : factor <= 5 ? 5 : 10) * magnitude;
    for (let v = Math.ceil(min / step) * step; v <= max + step * 1e-6; v += step) {
      const py = Math.round(y(v)) + 0.5;
      ctx.strokeStyle = Math.abs(v) < step * 1e-6 ? colors.muted : colors.line;
      ctx.beginPath(); ctx.moveTo(left, py); ctx.lineTo(width - right, py); ctx.stroke();
      ctx.fillStyle = colors.muted; ctx.textAlign = 'right';
      ctx.fillText((step < 1 ? v.toFixed(1) : Math.round(v)) + ' €', left - 8, py);
    }
    const stamp = span < 172800000 ? {day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit'} : {day: '2-digit', month: '2-digit'};
    ctx.textAlign = 'center'; ctx.textBaseline = 'top'; ctx.fillStyle = colors.muted;
    [0, 0.5, 1].forEach((f, i) => {
      ctx.textAlign = i === 0 ? 'left' : i === 2 ? 'right' : 'center';
      ctx.fillText(new Date(start + span * f).toLocaleString('fr-FR', stamp), left + (width - left - right) * f, height - bottom + 9);
    });
    [['depenses', colors.cost, [6, 4], 2], ['revenus', colors.revenue, [], 2], ['benefice', colors.profit, [2, 3], 2.5]].forEach(([key, color, dash, size]) => {
      ctx.strokeStyle = color; ctx.lineWidth = size; ctx.setLineDash(dash);
      ctx.beginPath(); ctx.moveTo(x(start), y(0));
      let last = 0;
      points.forEach((p, i) => { ctx.lineTo(x(times[i]), y(last)); ctx.lineTo(x(times[i]), y(p[key])); last = p[key]; });
      ctx.lineTo(x(end), y(last)); ctx.stroke(); ctx.setLineDash([]);
    });
    const final = points[points.length - 1];
    canvas.setAttribute('aria-label', `Courbe cumulée : dépenses ${euro(final.depenses)}, revenus ${euro(final.revenus)}, bénéfice ${euro(final.benefice)}.`);
  }

  function context(id) {
    const offer = store.offers.find(o => o.id === id);
    if (offer) return offer;
    const trade = store.ledger?.trades.find(t => t.item_id === id);
    return trade ? {id, title: trade.title, price: trade.buy_price, trade} : null;
  }

  function openTrade(id, mode) {
    const o = context(id);
    if (!o) return;
    const t = o.trade, form = $('#trade-form');
    form.dataset.id = id;
    form.dataset.mode = mode;
    $('#trade-title').textContent = mode === 'buy' ? (t ? 'Modifier l’achat' : 'Article acheté') : 'Article vendu';
    $('#trade-item').textContent = o.title;
    $('#trade-buy-field').hidden = mode === 'sell';
    $('#trade-buy').required = mode === 'buy';
    $('#trade-sell').required = mode === 'sell';
    $('#trade-sell-label').firstChild.textContent = mode === 'buy' ? 'Prix de revente, si déjà revendu (€)' : 'Prix de revente (€)';
    $('#trade-buy').value = t ? t.buy_price : o.price;
    $('#trade-sell').value = t && t.sell_price != null ? t.sell_price : '';
    $('#trade-platform').value = t ? t.platform : '';
    $('#trade-fees').value = t && t.fees ? t.fees : '';
    $('#trade-error').hidden = true;
    $('#trade-dialog').showModal();
    (mode === 'buy' ? $('#trade-buy') : $('#trade-sell')).focus();
  }

  $('#trade-form').addEventListener('submit', async event => {
    event.preventDefault();
    const form = event.currentTarget, button = $('#trade-submit');
    const body = {id: form.dataset.id, action: form.dataset.mode, platform: $('#trade-platform').value, fees: $('#trade-fees').value || null};
    if (body.action === 'buy') { body.buy_price = $('#trade-buy').value; body.sell_price = $('#trade-sell').value || null; }
    else body.sell_price = $('#trade-sell').value;
    button.disabled = true;
    try {
      await post('/api/trade', body);
      $('#trade-dialog').close();
      await Promise.all([refreshOffers(), refreshLedger()]);
      toast(body.action === 'buy' ? 'Achat enregistré.' : 'Vente enregistrée.');
    } catch (error) {
      $('#trade-error').textContent = error.message;
      $('#trade-error').hidden = false;
    } finally {
      button.disabled = false;
    }
  });

  document.addEventListener('click', async event => {
    const button = event.target.closest('button');
    if (!button) return;
    if (button.dataset.trade) openTrade(button.dataset.id, button.dataset.trade);
    else if (button.dataset.retry) {
      try { await post('/api/appraisal/retry', {id: button.dataset.retry}); await refreshOffers(); toast('Évaluation relancée.'); }
      catch (error) { toast(error.message, true); }
    } else if (button.dataset.more) {
      if (button.dataset.more === 'offers') { store.shown += PAGE; renderOffers(); }
      else { store.tradesShown += TRADE_PAGE; renderLedger(); }
    } else if (button.dataset.reload) {
      store.offersRev = store.ledgerRev = null;
      await (button.dataset.reload === 'offers' ? refreshOffers() : refreshLedger());
    }
  });

  let filterTimer;
  ['#offers-brand', '#offers-size', '#offers-status', '#offers-sort', '#offers-min', '#offers-confidence', '#offers-search'].forEach(selector => $(selector).addEventListener('input', () => {
    clearTimeout(filterTimer);
    filterTimer = setTimeout(() => { store.shown = PAGE; renderOffers(); }, selector === '#offers-search' || selector.includes('min') || selector.includes('confidence') ? 150 : 0);
  }));
  if (matchMedia('(max-width: 600px)').matches) $('#offers-filter-box').open = false;
  window.addEventListener('resize', () => { if (view.name === 'ledger') drawChart(); });
  new MutationObserver(() => { if (view.name === 'ledger') drawChart(); }).observe(document.documentElement, {attributes: true, attributeFilter: ['data-theme', 'data-accent']});

  function poll() {
    if (document.hidden) return;
    if (view.name === 'contemporary') refreshOffers();
    if (view.name === 'ledger') refreshLedger();
  }

  return {
    show(name) {
      view.name = name;
      store.shown = PAGE;
      store.tradesShown = TRADE_PAGE;
      clearInterval(view.timer);
      view.timer = setInterval(poll, POLL_MS);
      store.offersRev = store.ledgerRev = null;
      name === 'contemporary' ? (renderOffers(), refreshOffers()) : (renderLedger(), refreshLedger());
    },
    hide() {
      view.name = null;
      clearInterval(view.timer);
    },
  };
}
