'use strict';

const $ = (sel) => document.querySelector(sel);
const api = async (url, opts) => {
  const r = await fetch(url, opts);
  if (!r.ok) throw new Error(await r.text());
  return r.json();
};

const DECISIONS = { participate: 'участвуем', skip: 'пропущен' };

let decision = 'new';
let currentLot = null;
let poll = null;

// --- статус и сбор -------------------------------------------------------

async function refreshState() {
  const s = await api('/api/state');
  const p = s.profile;
  $('#profile-info').textContent =
    `профиль ${p.version} · ${p.keywords} слов · ${p.groups} групп · заказчики: ${p.organizers.join(', ')}`;
  $('#c-new').textContent = s.counts.new;
  $('#c-all').textContent = s.counts.lots;

  const job = s.job;
  const running = job.status === 'running';
  $('#collect').disabled = running;
  $('#cancel').hidden = !running;

  if (running) {
    $('#job').textContent = job.message;
  } else if (job.status === 'error') {
    $('#job').textContent = 'ошибка: ' + job.error;
  } else if (job.stats && Object.keys(job.stats).length) {
    const st = job.stats;
    $('#job').textContent =
      `${st.saved_lots} лотов из ${st.active} актуальных · ${st.calls} запросов`;
  } else if (s.last_run && s.last_run.finished) {
    $('#job').textContent = 'последний сбор: ' + s.last_run.finished.replace('T', ' ').slice(0, 16);
  }

  if (running && !poll) poll = setInterval(refreshState, 1200);
  if (!running && poll) { clearInterval(poll); poll = null; loadList(); }
  return s;
}

$('#collect').onclick = async () => { await api('/api/collect', { method: 'POST' }); refreshState(); };
$('#cancel').onclick = async () => { await api('/api/collect/cancel', { method: 'POST' }); refreshState(); };

// --- список --------------------------------------------------------------

document.querySelectorAll('#tabs button').forEach((b) => {
  b.onclick = () => {
    document.querySelectorAll('#tabs button').forEach((x) => x.classList.remove('on'));
    b.classList.add('on');
    decision = b.dataset.decision;
    loadList();
  };
});

function deadlineTag(row) {
  if (row.days_left === null || row.days_left === undefined) return '<span class="tag">срок не указан</span>';
  const cls = row.days_left <= 3 ? 'deadline-soon' : 'deadline-ok';
  return `<span class="tag ${cls}">${row.days_left} раб. дн.</span>`;
}

function money(v) {
  if (v === null || v === undefined) return '—';
  return Number(v).toLocaleString('ru-RU', { maximumFractionDigits: 2 });
}

async function loadList() {
  const rows = await api('/api/lots?decision=' + encodeURIComponent(decision));
  const box = $('#list');
  if (!rows.length) {
    box.innerHTML = '<div class="empty">Пусто. Нажмите «Собрать тендеры».</div>';
    return;
  }
  box.innerHTML = rows.map((r) => `
    <div class="row${currentLot === r.id ? ' on' : ''}" data-id="${r.id}">
      <div class="title">${esc(r.title || r.purchase_title || '—')}</div>
      <div class="meta">
        ${deadlineTag(r)}
        <span class="tag ${r.grp === 'Заказчик под наблюдением' ? 'watch' : 'grp'}">${esc(r.grp || '')}</span>
        <span>${esc(short(r.organizer, 40))}</span>
        <span>${money(r.price)} BYN${r.volume ? ' / ' + money(r.volume) + ' ' + esc(r.unit || '') : ''}</span>
        ${r.files_count ? `<span>📎 ${r.files_count}</span>` : ''}
      </div>
    </div>`).join('');
  box.querySelectorAll('.row').forEach((el) => {
    el.onclick = () => openLot(el.dataset.id);
  });
}

// --- карточка ------------------------------------------------------------

async function openLot(id) {
  currentLot = id;
  document.querySelectorAll('.row').forEach((el) =>
    el.classList.toggle('on', el.dataset.id === id));
  const r = await api('/api/lots/' + id);

  const files = (r.files || []).map((f) => `
    <li data-idx="${f.idx}">
      <span>${esc(f.name || 'файл')} <span class="kind">${ext(f.name)}</span></span>
      <span class="file-actions">
        <span class="file-status ${f.status === 'готов' ? 'ok' : 'warn'}">${f.local ? 'скачан' : esc(f.status === 'new' ? '' : f.status || '')}</span>
        ${f.local
          ? `<a href="/api/files/${esc(r.purchase_id)}/${f.idx}" target="_blank" rel="noopener">открыть</a>`
          : `<a href="${esc(f.url)}" target="_blank" rel="noopener">на площадке</a>`}
        <button class="dl" data-idx="${f.idx}">скачать</button>
      </span>
    </li>`).join('') || '<li class="muted">Документы к закупке не приложены</li>';

  const siblings = (r.siblings || []).map((s) => `
    <tr${s.id === r.id ? ' style="background:#eef3ff"' : ''}>
      <td>${s.lot_number ?? ''}</td>
      <td>${esc(s.title || '')}</td>
      <td class="num">${money(s.volume)} ${esc(s.unit || '')}</td>
      <td class="num">${money(s.price)}</td>
    </tr>`).join('');

  $('#card-pane').innerHTML = `
    <h1>${esc(r.title || '—')}</h1>
    <div class="card-sub">${esc(r.purchase_title || '')} · №${esc(r.number || '')} · ${esc(r.tender_form || '')}</div>

    <div class="facts">
      <div class="fact"><div class="k">Окончание подачи</div><div class="v">${esc(r.deadline || '—')}</div></div>
      <div class="fact"><div class="k">Запас</div><div class="v">${r.days_left ?? '—'} раб. дн.</div></div>
      <div class="fact"><div class="k">Сумма лота</div><div class="v">${money(r.price)} BYN</div></div>
      <div class="fact"><div class="k">Цена за ${esc(r.unit || 'ед.')}</div><div class="v">${money(r.price_per_unit)} BYN</div></div>
      <div class="fact"><div class="k">Количество</div><div class="v">${money(r.volume)} ${esc(r.unit || '')}</div></div>
      <div class="fact"><div class="k">Состояние</div><div class="v" style="font-size:13px">${esc(r.purchase_state || '')}</div></div>
    </div>

    <h2>Заказчик</h2>
    <div>${esc(r.organizer || '—')}${r.unp ? ' · УНП ' + esc(r.unp) : ''}</div>
    <div class="muted small">${esc(r.location || '')}</div>

    <h2>Почему отобрано</h2>
    <div>${esc(r.grp || '')} — ${esc(r.reason || '')}</div>
    ${r.keywords ? `<div class="muted small">совпало: ${esc(r.keywords)}</div>` : ''}
    ${r.okpb ? `<div class="muted small">ОКПБ: ${esc(r.okpb)}</div>` : ''}

    <h2>Лоты закупки</h2>
    <table><thead><tr><th>№</th><th>Наименование</th><th class="num">Количество</th><th class="num">Сумма, BYN</th></tr></thead>
    <tbody>${siblings}</tbody></table>

    <h2>Документация
      ${(r.files || []).length ? '<button id="dl-all" class="ghost small-btn">скачать всё</button>' : ''}
    </h2>
    <ul class="files">${files}</ul>

    <h2>Ссылки</h2>
    <div>
      ${r.auction_url ? `<a href="${esc(r.auction_url)}" target="_blank" rel="noopener">процедура на площадке</a> · ` : ''}
      <a href="${esc(r.page_url)}" target="_blank" rel="noopener">карточка в ГИАС</a>
    </div>

    ${r.delivery ? `<h2>Поставка</h2><div>${esc(r.delivery)}</div>` : ''}

    <div class="decide">
      <button class="yes" data-d="participate">Участвуем</button>
      <button class="no" data-d="skip">Пропустить</button>
    </div>
    <div class="decided">${r.decision ? 'Решение: ' + DECISIONS[r.decision] : 'Решение не принято'}</div>
  `;

  $('#card-pane').querySelectorAll('.dl').forEach((b) => {
    b.onclick = () => downloadFile(r.purchase_id, b.dataset.idx, b);
  });
  const all = $('#dl-all');
  if (all) {
    all.onclick = async () => {
      all.disabled = true;
      all.textContent = 'качаю…';
      const res = await api(`/api/purchases/${r.purchase_id}/download`, { method: 'POST' });
      all.disabled = false;
      all.textContent = 'скачать всё';
      if (res.failed) alert(`Скачано ${res.downloaded} из ${res.total}. ${res.status}`);
      openLot(id);
    };
  }

  $('#card-pane').querySelectorAll('.decide button').forEach((b) => {
    b.onclick = async () => {
      await api(`/api/lots/${id}/decision`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ decision: b.dataset.d }),
      });
      await refreshState();
      await loadList();
      openLot(id);
    };
  });
}

async function downloadFile(purchaseId, idx, btn) {
  const li = btn.closest('li');
  const status = li.querySelector('.file-status');
  btn.disabled = true;
  status.className = 'file-status warn';
  status.textContent = 'качаю…';
  try {
    const res = await api(`/api/files/${purchaseId}/${idx}/download`, { method: 'POST' });
    if (res.ok) {
      status.className = 'file-status ok';
      status.textContent = `скачан, ${Math.round((res.size || 0) / 1024)} КБ`;
      const link = li.querySelector('a');
      link.href = `/api/files/${purchaseId}/${idx}`;
      link.textContent = 'открыть';
    } else {
      status.className = 'file-status err';
      status.textContent = res.status;
    }
  } catch (e) {
    status.className = 'file-status err';
    status.textContent = 'ошибка запроса';
  }
  btn.disabled = false;
}

// --- настройки -----------------------------------------------------------

$('#open-settings').onclick = async () => {
  const s = await api('/api/settings');
  $('#s-window').value = s.window_days;
  $('#s-mindays').value = s.min_working_days;
  $('#s-pause').value = s.request_pause;
  $('#s-proxy').value = s.proxy || '';
  $('#src-gias').checked = !!(s.sources || {}).gias;
  $('#src-icetrade').checked = !!(s.sources || {}).icetrade;
  $('#icetrade-status').textContent = '';
  $('#s-holidays').value = (s.holidays || []).join(', ');
  $('#s-weekends').value = (s.working_weekends || []).join(', ');
  $('#thresholds').innerHTML = Object.entries(s.price_thresholds).map(([k, v]) => `
    <div class="thr" data-k="${esc(k)}">
      <span>${esc(k)}</span>
      <input type="number" step="0.1" class="green" value="${v.green}" title="зелёный, не дороже">
      <input type="number" step="0.1" class="red" value="${v.red}" title="красный, дороже или равно">
    </div>`).join('');
  $('#settings').showModal();
};

$('#save-settings').onclick = async () => {
  const thresholds = {};
  document.querySelectorAll('#thresholds .thr').forEach((el) => {
    thresholds[el.dataset.k] = {
      green: Number(el.querySelector('.green').value),
      red: Number(el.querySelector('.red').value),
    };
  });
  const list = (s) => s.split(',').map((x) => x.trim()).filter(Boolean);
  await api('/api/settings', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      window_days: Number($('#s-window').value),
      min_working_days: Number($('#s-mindays').value),
      request_pause: Number($('#s-pause').value),
      proxy: $('#s-proxy').value.trim(),
      sources: { gias: $('#src-gias').checked, icetrade: $('#src-icetrade').checked },
      price_thresholds: thresholds,
      holidays: list($('#s-holidays').value),
      working_weekends: list($('#s-weekends').value),
    }),
  });
  refreshState();
};

$('#check-icetrade').onclick = async () => {
  const out = $('#icetrade-status');
  out.textContent = ' проверяю…';
  out.className = '';
  // Прокси мог быть только что вписан и ещё не сохранён — сохраняем перед проверкой.
  await api('/api/settings', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ proxy: $('#s-proxy').value.trim() }),
  });
  try {
    const r = await api('/api/sources/icetrade/check', { method: 'POST' });
    out.className = r.ok ? 'ok-text' : 'err-text';
    out.textContent = r.ok
      ? ` доступен, страница ${r.size} байт${r.proxy ? ' (через прокси)' : ''}`
      : ' ' + r.reason;
  } catch (e) {
    out.className = 'err-text';
    out.textContent = ' ошибка запроса';
  }
};

// --- мелочи --------------------------------------------------------------

function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}
function short(s, n) {
  s = String(s ?? '');
  return s.length > n ? s.slice(0, n - 1) + '…' : s;
}
function ext(name) {
  const m = String(name ?? '').match(/\.([a-z0-9]+)$/i);
  return m ? m[1].toUpperCase() : '';
}

refreshState().then(loadList);
