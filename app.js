// Equipment Runtime Monitor -- dashboard frontend.
//
// The server (equipment_monitor.py) always sends timestamps as UTC
// ISO-8601 strings ("2026-09-23T00:08:53Z"). Every place this file shows
// a timestamp to a person, it converts that into the viewer's own local
// time via the Date object -- so two people looking at the same dashboard
// from different timezones each see times that make sense to them.

function esc(s) {
  return String(s).replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;');
}

// "2026-09-23T00:08:53Z" (or the legacy "2026-09-23 00:08:53", assumed
// UTC) -> a Date object.
function parseServerTime(s) {
  if (!s) return null;
  const iso = /Z$|[+-]\d\d:\d\d$/.test(s) ? s : s.replace(' ', 'T') + 'Z';
  const d = new Date(iso);
  return isNaN(d.getTime()) ? null : d;
}

// Renders in the viewer's local timezone, formatted like the original
// "YYYY-MM-DD HH:MM:SS" so the display is familiar either way.
function fmtLocal(s) {
  const d = parseServerTime(s);
  if (!d) return s || '';
  const pad = n => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

function localDayStart(s) {
  const d = parseServerTime(s);
  if (!d) return null;
  return new Date(d.getFullYear(), d.getMonth(), d.getDate());
}

function prettyRaw(raw) {
  try {
    return JSON.stringify(JSON.parse(raw), null, 2);
  } catch (e) {
    return raw;
  }
}

// ---- 24-hour ON/OFF timeline, drawn as a digital "square wave": X axis
// is time of day, Y axis is two levels (ON high / OFF low). Green while
// running, red while idle; the vertical jumps mark every start/stop. ----
function buildTimelineSVG(sessionsAsc, lastSeenStr) {
  const W = 1040, H = 190;
  const left = 52, right = W - 14, top = 14, bottom = H - 34;
  const onY = top + 22;
  const offY = bottom - 4;
  const dayMs = 24 * 3600 * 1000;

  if (!sessionsAsc.length) {
    return {
      svg: `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" role="img" aria-label="No timeline data yet"></svg>`,
      label: 'no data yet',
    };
  }

  const dayStart = localDayStart(sessionsAsc[0].start);
  const xAt = ms => left + Math.max(0, Math.min(1, ms / dayMs)) * (right - left);

  let lastSeen = parseServerTime(lastSeenStr);
  let nowMs = lastSeen ? (lastSeen - dayStart) : null;
  if (nowMs != null && (nowMs < 0 || nowMs > dayMs)) nowMs = null;

  // Build the step path as a flat list of points, starting OFF at
  // midnight, rising to ON at each session start, falling to OFF at each
  // session end, and holding OFF until the next session or "now".
  const points = [[0, offY]];
  let cursorMs = 0;
  const greenSegs = [];
  const redSegs = [];

  sessionsAsc.forEach(s => {
    const startD = parseServerTime(s.start);
    const endD = parseServerTime(s.end);
    if (!startD || !endD) return;
    let sMs = startD - dayStart;
    let eMs = Math.max(sMs, endD - dayStart);
    sMs = Math.max(0, Math.min(dayMs, sMs));
    eMs = Math.max(0, Math.min(dayMs, eMs));

    if (sMs > cursorMs) redSegs.push([cursorMs, sMs]);
    points.push([sMs, offY], [sMs, onY]);
    greenSegs.push([sMs, eMs]);
    points.push([eMs, onY], [eMs, offY]);
    cursorMs = eMs;
  });

  const tailEnd = nowMs != null ? Math.max(cursorMs, nowMs) : dayMs;
  if (tailEnd > cursorMs) redSegs.push([cursorMs, tailEnd]);

  const segLine = (a, b, y, color) =>
    `<line x1="${xAt(a).toFixed(1)}" y1="${y}" x2="${xAt(b).toFixed(1)}" y2="${y}" stroke="${color}" stroke-width="3" stroke-linecap="round"/>`;
  const riseFall = (ms, y1, y2, color) =>
    `<line x1="${xAt(ms).toFixed(1)}" y1="${y1}" x2="${xAt(ms).toFixed(1)}" y2="${y2}" stroke="${color}" stroke-width="3"/>`;

  const parts = [];
  // Gridlines for the two Y levels.
  parts.push(`<line x1="${left}" y1="${onY}" x2="${right}" y2="${onY}" stroke="#1f4a34" stroke-width="1" stroke-dasharray="4 4"/>`);
  parts.push(`<line x1="${left}" y1="${offY}" x2="${right}" y2="${offY}" stroke="#1f4a34" stroke-width="1"/>`);
  // Y axis.
  parts.push(`<line x1="${left}" y1="${top - 4}" x2="${left}" y2="${offY + 4}" stroke="#2a5a3e" stroke-width="1.5"/>`);
  parts.push(`<text x="${left - 8}" y="${onY + 4}" text-anchor="end" font-size="12" fill="#7fd8a0">ON</text>`);
  parts.push(`<text x="${left - 8}" y="${offY + 4}" text-anchor="end" font-size="12" fill="#e78b8b">OFF</text>`);

  redSegs.forEach(([a, b]) => parts.push(segLine(a, b, offY, '#e74c3c')));
  greenSegs.forEach(([a, b]) => parts.push(segLine(a, b, onY, '#2ecc71')));
  // Rising/falling edges, colored by the state they enter.
  for (let i = 1; i < points.length - 1; i += 2) {
    const [ms, yFrom] = points[i];
    const [, yTo] = points[i + 1] || points[i];
    if (yTo === onY) parts.push(riseFall(ms, yFrom, yTo, '#2ecc71'));
    else parts.push(riseFall(ms, yFrom, yTo, '#e74c3c'));
  }

  // X axis ticks: 00:00, 06:00, 12:00, 18:00, 24:00.
  [0, 6, 12, 18, 24].forEach(h => {
    const x = xAt(h * 3600 * 1000);
    parts.push(`<line x1="${x.toFixed(1)}" y1="${offY}" x2="${x.toFixed(1)}" y2="${offY + 5}" stroke="#2a5a3e" stroke-width="1.5"/>`);
    const anchor = h === 0 ? 'start' : (h === 24 ? 'end' : 'middle');
    parts.push(`<text x="${x.toFixed(1)}" y="${offY + 20}" text-anchor="${anchor}" font-size="12" fill="#6c8977">${String(h).padStart(2, '0')}:00</text>`);
  });

  if (nowMs != null) {
    const x = xAt(nowMs);
    parts.push(`<line x1="${x.toFixed(1)}" y1="${top - 6}" x2="${x.toFixed(1)}" y2="${offY + 6}" stroke="#eaf2ec" stroke-width="1.5" stroke-dasharray="3 3"/>`);
  }

  const svg = `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" role="img" aria-label="24 hour ON/OFF timeline">${parts.join('')}</svg>`;
  return { svg, label: dayStart.toLocaleDateString() };
}

// ---- Raw MQTT drill-down modal ----
const modal = document.getElementById('raw-modal');
const modalBody = document.getElementById('raw-modal-body');
const modalSub = document.getElementById('raw-modal-sub');

function closeModal() {
  modal.hidden = true;
  modalBody.innerHTML = '';
}

document.getElementById('raw-modal-close').addEventListener('click', closeModal);
modal.addEventListener('click', e => { if (e.target === modal) closeModal(); });
document.addEventListener('keydown', e => { if (e.key === 'Escape' && !modal.hidden) closeModal(); });

async function openSessionRaw(start, end) {
  modal.hidden = false;
  modalSub.textContent = `${fmtLocal(start)} \u2192 ${fmtLocal(end)}`;
  modalBody.innerHTML = '<p class="meta">Loading raw messages&hellip;</p>';
  try {
    const res = await fetch(`/api/session_raw?start=${encodeURIComponent(start)}&end=${encodeURIComponent(end)}`);
    const data = await res.json();
    const hits = data.hits || [];
    if (!hits.length) {
      modalBody.innerHTML = '<p class="meta">No raw MQTT messages found for this session.</p>';
      return;
    }
    modalBody.innerHTML = hits.map(h => `
      <div class="raw-hit">
        <div class="raw-hit-head"><strong>${esc(fmtLocal(h.when))}</strong><span>topic: ${esc(h.topic || '')}</span></div>
        <pre>${esc(prettyRaw(h.raw))}</pre>
      </div>
    `).join('');
  } catch (e) {
    modalBody.innerHTML = '<p class="meta">Could not load raw messages.</p>';
  }
}

// ---- Main refresh loop ----
async function refresh() {
  let d;
  try {
    d = await (await fetch('/api/motion')).json();
  } catch (e) {
    document.getElementById('status-text').textContent = 'Cannot reach server';
    return;
  }

  document.getElementById('mac-label').textContent = d.mac;

  const st = d.state || {};
  const dot = document.getElementById('status-dot');
  dot.className = 'status-dot ' + (st.state || 'unknown').toLowerCase();
  document.getElementById('status-text').textContent = st.state || 'Unknown';
  const metaBits = [];
  if (st.last_seen) metaBits.push('last seen ' + fmtLocal(st.last_seen));
  if (st.battery_percent != null) metaBits.push('sensor battery ' + st.battery_percent + '%');
  if (st.energy_percent != null) metaBits.push('energy ' + st.energy_percent + '%');
  document.getElementById('status-meta').textContent = metaBits.join(' \u00b7 ') || (d.event_count === 0 ? 'No messages captured yet' : '');

  const sum = d.summary || {};
  document.getElementById('total-run').textContent = sum.total_run_human || '0s';
  document.getElementById('availability').textContent = (sum.utilization_percent != null ? sum.utilization_percent + '%' : '\u2014');
  document.getElementById('cycles').textContent = sum.cycle_count != null ? sum.cycle_count : '\u2014';
  document.getElementById('avg-session').textContent = sum.avg_session_human || '\u2014';

  document.getElementById('pm-hours-since').textContent = sum.hours_since_service != null ? sum.hours_since_service + 'h' : '\u2014';
  document.getElementById('pm-hours-until').textContent = sum.hours_until_service != null ? sum.hours_until_service + 'h' : '\u2014';
  document.getElementById('pm-cost').textContent = sum.estimated_downtime_cost != null ? ('$' + sum.estimated_downtime_cost.toLocaleString()) : 'not configured';
  const battEl = document.getElementById('pm-battery');
  if (st.battery_percent != null) {
    battEl.textContent = st.battery_percent + '%';
    battEl.className = 'n ' + (st.battery_percent < d.low_battery_threshold ? 'bad' : 'ok');
  } else {
    battEl.textContent = '\u2014';
    battEl.className = 'n';
  }

  const banner = document.getElementById('service-banner');
  banner.innerHTML = sum.service_due
    ? '<div class="banner due"><strong>Service due:</strong> this machine has run ' + sum.hours_since_service + 'h since its last service, past the ' + sum.service_interval_hours + 'h interval.</div>'
    : '';

  const anomalies = d.anomalies || [];
  document.getElementById('anomaly-count').textContent = anomalies.length;
  document.getElementById('anomaly-empty').hidden = anomalies.length > 0;
  document.getElementById('anomaly-rows').innerHTML = anomalies.map(a =>
    '<tr><td>' + esc(fmtLocal(a.when)) + '</td><td><span class="kind-tag kind-' + esc(a.kind) + '">' + esc(a.kind) + '</span></td><td>' + esc(a.message) + '</td></tr>'
  ).join('');

  const sessions = d.sessions || []; // newest first, as sent by the server
  document.getElementById('session-count').textContent = sessions.length + ' session' + (sessions.length === 1 ? '' : 's');
  document.getElementById('sessions-empty').hidden = sessions.length > 0;
  const sessionRows = document.getElementById('session-rows');
  sessionRows.innerHTML = sessions.map((s, i) =>
    `<tr class="session-row" tabindex="0" data-idx="${i}"><td>${esc(fmtLocal(s.start))}</td><td>${esc(fmtLocal(s.end))}</td><td>${esc(s.duration_human)}</td><td>${s.readings}</td></tr>`
  ).join('');
  sessionRows.querySelectorAll('tr.session-row').forEach(row => {
    const s = sessions[Number(row.dataset.idx)];
    const open = () => openSessionRaw(s.start, s.end);
    row.addEventListener('click', open);
    row.addEventListener('keydown', e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); open(); } });
  });

  const sessionsAsc = sessions.slice().reverse();
  const { svg, label } = buildTimelineSVG(sessionsAsc, st.last_seen);
  document.getElementById('timeline-chart').innerHTML = svg;
  document.getElementById('timeline-meta').textContent = label;
}

refresh();
setInterval(refresh, 3000);
