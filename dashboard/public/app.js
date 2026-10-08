// Spotify review insights dashboard. Plain ES modules, no build step.
// All data comes from the serverless API (/api/*), which reads PostgreSQL.
// Untrusted text (review text, model prose) is always inserted with textContent.

const fmt = new Intl.NumberFormat('en-US');
const pct = (x, d = 1) => (x == null ? '—' : `${(x * 100).toFixed(d)}%`);
const $ = (sel) => document.querySelector(sel);

function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === 'class') el.className = v;
    else if (k.startsWith('on')) el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v === true ? '' : v);
  }
  for (const c of children.flat()) {
    if (c == null || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}
const svgNS = 'http://www.w3.org/2000/svg';
function s(tag, attrs = {}, ...children) {
  const el = document.createElementNS(svgNS, tag);
  for (const [k, v] of Object.entries(attrs)) if (v != null) el.setAttribute(k, v);
  for (const c of children.flat()) if (c != null) el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  return el;
}

async function api(path) {
  const res = await fetch(`/api/${path}`, { headers: { Accept: 'application/json' } });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.error || `HTTP ${res.status}`);
  return body;
}

function check(ok, yes = 'matches', no = 'mismatch') {
  return h('span', { class: ok ? 'ok' : 'bad' }, ok ? yes : no);
}

// Highlight the exact evidence quote inside the review text (both untrusted).
function quoted(text, quote) {
  const frag = document.createDocumentFragment();
  const i = quote ? text.indexOf(quote) : -1;
  if (i < 0) { frag.append(text); return frag; }
  frag.append(text.slice(0, i), h('mark', {}, quote), text.slice(i + quote.length));
  return frag;
}

function source(file) {
  return h('p', { class: 'dim', style: 'font-size:12.5px;margin:10px 0 0' }, 'Source: ', h('code', {}, file));
}

// ---------- tooltip ----------
const tip = $('#tooltip');
function showTip(evt, nodes) {
  tip.replaceChildren(...nodes);
  tip.style.display = 'block';
  const pad = 12;
  const { innerWidth: W } = window;
  const r = tip.getBoundingClientRect();
  let x = evt.clientX + pad;
  if (x + r.width > W - 8) x = evt.clientX - r.width - pad;
  tip.style.left = `${x + window.scrollX}px`;
  tip.style.top = `${evt.clientY + window.scrollY - r.height - pad}px`;
}
function hideTip() { tip.style.display = 'none'; }

// ---------- charts ----------
function niceMax(v) {
  const p = 10 ** Math.floor(Math.log10(v));
  for (const m of [1, 2, 2.5, 5, 10]) if (m * p >= v) return m * p;
  return 10 * p;
}

function topicChart(el, topics) {
  const W = Math.max(el.clientWidth, 300);
  const labelW = 86, valueW = 64, rowH = 30, barH = 16, top = 4;
  const H = top + topics.length * rowH + 4;
  const max = Math.max(...topics.map((t) => t.priority_score));
  const plotW = W - labelW - valueW;
  const svg = s('svg', { viewBox: `0 0 ${W} ${H}`, role: 'img', 'aria-label': 'Priority score by topic, horizontal bar chart' });
  svg.append(s('line', { class: 'baseline', x1: labelW, x2: labelW, y1: 0, y2: H }));
  topics.forEach((t, i) => {
    const y = top + i * rowH + (rowH - barH) / 2;
    const w = Math.max(2, (t.priority_score / max) * plotW);
    svg.append(s('text', { class: 'cat', x: labelW - 8, y: y + barH / 2 + 4, 'text-anchor': 'end' }, t.topic));
    svg.append(s('path', { class: 'bar', d: `M${labelW},${y}h${w - 4}a4,4 0 0 1 4,4v${barH - 8}a4,4 0 0 1 -4,4h${-(w - 4)}z` }));
    svg.append(s('text', { class: 'val', x: labelW + w + 6, y: y + barH / 2 + 4 }, fmt.format(t.priority_score)));
    const hit = s('rect', { x: 0, y: top + i * rowH, width: W, height: rowH, fill: 'transparent', tabindex: 0 });
    const tipNodes = () => [
      h('strong', {}, fmt.format(t.priority_score)),
      h('div', {}, `${t.topic} · priority score`),
      h('div', { class: 'dim' }, `${fmt.format(t.complaint_count)} complaints · mean severity ${t.mean_severity} · ${t.issue_count} issues`),
    ];
    hit.addEventListener('pointermove', (e) => showTip(e, tipNodes()));
    hit.addEventListener('pointerleave', hideTip);
    svg.append(hit);
  });
  el.replaceChildren(svg);
}

function lineChart(el, months) {
  const W = Math.max(el.clientWidth, 300), H = 190;
  const m = { l: 40, r: 12, t: 10, b: 26 };
  const pw = W - m.l - m.r, ph = H - m.t - m.b;
  const vals = months.map((d) => d.complaint_share);
  const ymax = Math.min(1, niceMax(Math.max(...vals) * 1.1));
  const x = (i) => m.l + (i / (months.length - 1)) * pw;
  const y = (v) => m.t + ph - (v / ymax) * ph;
  const svg = s('svg', { viewBox: `0 0 ${W} ${H}`, role: 'img', 'aria-label': 'Monthly complaint share, line chart' });
  for (let k = 0; k <= 4; k++) {
    const v = (ymax / 4) * k;
    svg.append(s('line', { class: k === 0 ? 'baseline' : 'gridline', x1: m.l, x2: W - m.r, y1: y(v), y2: y(v) }));
    svg.append(s('text', { x: m.l - 6, y: y(v) + 4, 'text-anchor': 'end' }, `${Math.round(v * 100)}%`));
  }
  months.forEach((d, i) => {
    const lastI = months.length - 1;
    if ((i % 3 === 0 && lastI - i >= 2) || i === lastI) svg.append(s('text', { x: x(i), y: H - 8, 'text-anchor': i === lastI ? 'end' : i === 0 ? 'start' : 'middle' }, d.month));
  });
  const path = months.map((d, i) => `${i ? 'L' : 'M'}${x(i).toFixed(1)},${y(d.complaint_share).toFixed(1)}`).join('');
  svg.append(s('path', { class: 'area', d: `${path}L${x(months.length - 1)},${y(0)}L${x(0)},${y(0)}Z` }));
  svg.append(s('path', { class: 'line', d: path }));
  const last = months.length - 1;
  svg.append(s('circle', { class: 'dot', cx: x(last), cy: y(months[last].complaint_share), r: 4 }));
  const cross = s('line', { class: 'crosshair', y1: m.t, y2: m.t + ph, visibility: 'hidden' });
  const focus = s('circle', { class: 'dot', r: 4, visibility: 'hidden' });
  svg.append(cross, focus);
  const hit = s('rect', { x: m.l, y: m.t, width: pw, height: ph, fill: 'transparent' });
  hit.addEventListener('pointermove', (e) => {
    const r = svg.getBoundingClientRect();
    const px = ((e.clientX - r.left) / r.width) * W;
    const i = Math.max(0, Math.min(last, Math.round(((px - m.l) / pw) * last)));
    const d = months[i];
    cross.setAttribute('x1', x(i)); cross.setAttribute('x2', x(i)); cross.setAttribute('visibility', 'visible');
    focus.setAttribute('cx', x(i)); focus.setAttribute('cy', y(d.complaint_share)); focus.setAttribute('visibility', 'visible');
    showTip(e, [
      h('strong', {}, pct(d.complaint_share)),
      h('div', {}, h('span', { class: 'key' }), `${d.month} complaint share`),
      h('div', { class: 'dim' }, `${fmt.format(d.complaint_or_cancellation)} of ${fmt.format(d.completed)} classified reviews`),
    ]);
  });
  hit.addEventListener('pointerleave', () => { hideTip(); cross.setAttribute('visibility', 'hidden'); focus.setAttribute('visibility', 'hidden'); });
  svg.append(hit);
  el.replaceChildren(svg);
}

function volumeChart(el, months) {
  const W = Math.max(el.clientWidth, 300), H = 96;
  const m = { l: 40, r: 12, t: 18, b: 8 };
  const pw = W - m.l - m.r, ph = H - m.t - m.b;
  const max = niceMax(Math.max(...months.map((d) => d.source_rows)));
  const band = pw / months.length;
  const bw = Math.min(18, band - 2);
  const svg = s('svg', { viewBox: `0 0 ${W} ${H}`, role: 'img', 'aria-label': 'Monthly review volume, column chart' });
  svg.append(s('text', { x: m.l, y: 11 }, 'Reviews per month'));
  svg.append(s('text', { x: m.l - 6, y: m.t + 4, 'text-anchor': 'end' }, `${Math.round(max / 1000)}k`));
  svg.append(s('line', { class: 'gridline', x1: m.l, x2: W - m.r, y1: m.t, y2: m.t }));
  svg.append(s('line', { class: 'baseline', x1: m.l, x2: W - m.r, y1: m.t + ph, y2: m.t + ph }));
  months.forEach((d, i) => {
    const hgt = Math.max(2, (d.source_rows / max) * ph);
    const cx = m.l + i * band + band / 2;
    const y0 = m.t + ph;
    svg.append(s('path', { class: 'bar', opacity: 0.55, d: `M${cx - bw / 2},${y0}v${-(hgt - 4)}a4,4 0 0 1 4,-4h${bw - 8}a4,4 0 0 1 4,4v${hgt - 4}z` }));
    const hit = s('rect', { x: m.l + i * band, y: m.t, width: band, height: ph, fill: 'transparent' });
    hit.addEventListener('pointermove', (e) => showTip(e, [h('strong', {}, fmt.format(d.source_rows)), h('div', {}, `${d.month} reviews`)]));
    hit.addEventListener('pointerleave', hideTip);
    svg.append(hit);
  });
  el.replaceChildren(svg);
}

function table(headers, rows, numeric = []) {
  return h('table', {},
    h('thead', {}, h('tr', {}, headers.map((t, i) => h('th', { class: numeric.includes(i) ? 'num' : null }, t)))),
    h('tbody', {}, rows.map((r) => h('tr', {}, r.map((c, i) => h('td', { class: numeric.includes(i) ? 'num' : null }, c))))));
}

// ---------- sections ----------
function renderTiles(o) {
  const c = o.counts;
  const tiles = [
    ['Reviews accounted for', fmt.format(c.source_rows), 'every original review ID'],
    ['Classified', fmt.format(c.completed), `${pct(c.completed / (c.source_rows - c.quarantined), 2)} of nonempty reviews`],
    ['Quarantined', fmt.format(c.quarantined), 'empty review text'],
    ['Complaints & cancellations', fmt.format(c.complaint_or_cancellation), `in ${o.issues.total} issues (one issue each)`],
    ['Exact-text reuse', fmt.format(c.exact_text_reused_rows), 'rows reusing a validated result'],
  ];
  $('#tiles').replaceChildren(...tiles.map(([l, v, n]) =>
    h('div', { class: 'tile' }, h('div', { class: 'label' }, l), h('div', { class: 'value' }, v), h('div', { class: 'note' }, n))));
  const ing = o.ingestion?.value;
  const src = o.source?.value;
  $('#meta-row').replaceChildren(
    h('span', {}, `Source SHA-256 ${src?.analysis_sha256?.slice(0, 12)}…`),
    h('span', {}, `Reviews ${ing?.first_review?.slice(0, 10)} → ${ing?.last_review?.slice(0, 10)}`),
    h('span', {}, `Labels: ${o.label_sources.map((l) => `${fmt.format(l.n)} ${l.label_source === 'luna_teacher' ? 'Luna (teacher)' : 'local classifier'}`).join(' · ')}`),
  );
}

function claimsTable(claims, issueBtn) {
  const metrics = ['complaint_count', 'severity_sum', 'mean_severity', 'priority_score'];
  const byIssue = new Map();
  for (const c of claims) {
    if (!byIssue.has(c.issue_id)) byIssue.set(c.issue_id, {});
    byIssue.get(c.issue_id)[c.metric] = c;
  }
  const cell = (c) => (c ? h('span', { title: `${c.claim_id}: memo ${c.value} / database ${c.db_value}` },
    c.value, ' ', h('span', { class: c.matches_database ? 'ok' : 'bad' }, '')) : '—');
  return table(
    ['Issue', 'Complaints', 'Severity sum', 'Mean severity', 'Priority score', 'Claim IDs'],
    [...byIssue.entries()].map(([id, m]) => [
      issueBtn(id), ...metrics.map((k) => cell(m[k])),
      h('span', { class: 'mono dim' }, metrics.map((k) => m[k]?.claim_id).filter(Boolean).join(' ')),
    ]),
    [1, 2, 3, 4],
  );
}

function renderReco(r, openIssue) {
  const m = r.memo;
  const card = $('#reco');
  const issues = Object.fromEntries(r.issues.map((i) => [i.issue_id, i]));
  const top = issues[m.priority_issue_id];
  const matched = r.claims.filter((c) => c.matches_database).length;
  const issueBtn = (id) => h('button', { class: 'link-btn', type: 'button', onclick: () => openIssue(id) }, id);

  const evidenceIds = [...new Set(r.claims.filter((c) => c.issue_id === m.priority_issue_id).flatMap((c) => c.review_ids))];
  const evById = Object.fromEntries(r.evidence.map((e) => [e.review_id, e]));

  card.replaceChildren(
    h('div', { class: 'eyebrow' }, 'AI-generated recommendation · memo role'),
    h('p', { class: 'priority' }, m.priority),
    h('div', { class: 'chips' },
      h('span', { class: 'chip' }, 'Topic ', h('strong', {}, m.priority_topic)),
      h('span', { class: 'chip' }, 'Issue ', h('strong', {}, issueBtn(m.priority_issue_id)), top ? ` · ${top.issue_name}` : ''),
      h('span', { class: 'chip' }, 'Baseline rank ', h('strong', {}, top ? `#${top.rank}` : '—')),
      h('span', { class: 'chip' }, 'Cited numbers equal to database ', h('strong', {}, `${matched}/${r.claims.length}`), ' ', check(r.all_claims_match_database, 'yes', 'no')),
    ),
    h('p', {}, m.rationale),
    h('h3', {}, 'Cited numbers (claims.csv), recomputed from the database'),
    h('div', { class: 'table-wrap' }, claimsTable(r.claims, issueBtn)),
    h('h3', { style: 'margin-top:14px' }, `Representative reviews for ${m.priority_issue_id}`),
    ...evidenceIds.map((id) => {
      const e = evById[id];
      if (!e) return null;
      return h('blockquote', { class: 'ev' }, quoted(e.review_text, e.evidence_quote),
        h('span', { class: 'who' }, `Review ${id} · ${e.intent} · severity ${e.severity}`));
    }),
    h('h3', { style: 'margin-top:14px' }, 'Alternatives considered'),
    h('ul', { class: 'plain' }, m.alternatives.map((a, i) => {
      const id = m.alternative_issue_ids[i];
      const alt = issues[id];
      return h('li', {}, alt ? h('span', { class: 'dim' }, `#${alt.rank} · score ${fmt.format(alt.priority_score)} · `) : '', a);
    })),
    h('details', {}, h('summary', {}, 'How the baseline and sensitivity analysis were handled'),
      h('p', {}, m.baseline_top_handling), h('p', {}, m.sensitivity_interpretation)),
    h('details', {}, h('summary', {}, 'Limitations'), h('ul', { class: 'plain' }, m.limitations.map((l) => h('li', {}, l)))),
    h('p', { class: 'dim', style: 'font-size:12.5px;margin-top:12px' },
      `Model ${m.model} · request ${m.request_id} · `, r.provenance_note),
  );
}

let rankingData = [];
let rankFilter = 'all';
let rankShowAll = false;
function renderRanking(openIssue) {
  const topic = $('#topic-filter').value;
  const matching = rankingData.filter((r) => (rankFilter === 'all' || (r.coherent && r.actionable)) && (!topic || r.topic === topic));
  const rows = rankShowAll ? matching : matching.slice(0, 15);
  const max = Math.max(...rankingData.map((r) => r.priority_score));
  $('#rank-count').textContent = `${matching.length} of ${rankingData.length} issues`;
  const t = h('table', {},
    h('thead', {}, h('tr', {}, ['#', 'Issue', 'Topic', 'Complaints', 'Mean severity', 'Priority score', '', 'Grouping review', 'DB = export'].map((x, i) =>
      h('th', { class: [0, 3, 4, 5].includes(i) ? 'num' : null }, x)))),
    h('tbody', {}, rows.map((r) => h('tr', { class: 'clickable', tabindex: 0, onclick: () => openIssue(r.issue_id), onkeydown: (e) => { if (e.key === 'Enter') openIssue(r.issue_id); } },
      h('td', { class: 'num' }, r.rank),
      h('td', {}, h('div', { class: 'issue-name' }, r.issue_name), h('div', { class: 'dim mono' }, r.issue_id)),
      h('td', {}, r.topic),
      h('td', { class: 'num' }, fmt.format(r.complaint_count)),
      h('td', { class: 'num' }, r.mean_severity),
      h('td', { class: 'num' }, fmt.format(r.priority_score)),
      h('td', { class: 'barcell' }, h('div', { class: `inline-bar${r.coherent && r.actionable ? '' : ' muted'}` }, h('span', { style: `width:${(r.priority_score / max) * 100}%` }))),
      h('td', {}, h('span', { class: 'tag' }, r.coherent ? 'coherent' : 'low coherence'), h('span', { class: 'tag' }, r.actionable ? 'actionable' : 'generic')),
      h('td', {}, check(r.matches_saved_export, 'yes', 'no')),
    ))));
  const toggle = matching.length > 15
    ? h('button', { class: 'btn', type: 'button', style: 'margin-top:10px', onclick: () => { rankShowAll = !rankShowAll; renderRanking(openIssue); } },
      rankShowAll ? 'Show top 15' : `Show all ${matching.length}`)
    : null;
  $('#ranking-table').replaceChildren(t, ...(toggle ? [toggle] : []));
}

function renderQuality(o) {
  const panels = [];
  const v = o.verification?.value;
  if (v) {
    const L = v.metrics_by_final_source.local, U = v.metrics_by_final_source.luna;
    panels.push(h('div', { class: 'card' },
      h('h3', {}, 'Independent verification (blind re-labeling)'),
      h('p', { class: 'sub' }, `${L.distinct_texts + U.distinct_texts} distinct texts re-labeled by a separate verifier role that never saw the first prediction. Row-weighted agreement; ${v.disagreement_count} disagreements are listed in the receipt.`),
      h('div', { class: 'table-wrap' }, table(['Final label source', 'Topic', 'Intent', 'Severity exact', 'Quote supports label'], [
        [`Local classifier (${L.distinct_texts} texts)`, pct(L.row_weighted_topic_agreement), pct(L.row_weighted_intent_agreement), pct(L.row_weighted_severity_exact), pct(L.quote_supported_rate)],
        [`Luna labels, same-model retest (${U.distinct_texts})`, pct(U.row_weighted_topic_agreement), pct(U.row_weighted_intent_agreement), pct(U.row_weighted_severity_exact), pct(U.quote_supported_rate)],
      ], [1, 2, 3, 4])),
      h('p', { class: 'dim', style: 'font-size:12.5px' }, 'Agreement with the verifier model is not human accuracy. Luna-labelled rows verified by Luna measure test–retest consistency only.'),
      source(o.verification.source_file)));
  }
  const sens = o.sensitivity?.value?.rank_stability;
  if (sens) {
    panels.push(h('div', { class: 'card' },
      h('h3', {}, 'Sensitivity to classification error'),
      h('p', { class: 'sub' }, 'Verifier confusion rates were used to re-estimate counts in 1,000 bootstrap draws.'),
      h('ul', { class: 'plain' },
        h('li', {}, `Top topic stays `, h('strong', {}, sens.baseline_top_topic), ` in ${pct(sens.baseline_top_topic_bootstrap_top_probability, 0)} of draws.`),
        h('li', {}, `Top issue ${sens.baseline_top_issue} stays first in ${pct(sens.baseline_top_issue_bootstrap_top_probability, 1)} of draws; the generic, non-actionable ${sens.adjusted_top_issue} leads in ${pct(sens.adjusted_top_issue_bootstrap_top_probability, 1)}.`)),
      source(o.sensitivity.source_file)));
  }
  const g = o.golden_evaluation?.value;
  if (g) {
    const A = g.agreement || {}, S = g.by_label_source || {};
    const row = (label, x) => [label, pct(x.topic), pct(x.intent), pct(x.severity_exact), pct(x.severity_within_1)];
    const rows = [row(`All final labels (${g.cases})`, A)];
    if (S.local_classifier) rows.push(row(`Local classifier (${S.local_classifier.cases})`, S.local_classifier));
    if (S.luna_teacher) rows.push(row(`Luna labels (${S.luna_teacher.cases})`, S.luna_teacher));
    panels.push(h('div', { class: 'card' },
      h('h3', {}, 'Golden 50 (human labels)'),
      h('p', { class: 'sub' }, `Final labels of the 50 hand-labeled reviews. Sentiment MAE ${(A.sentiment_mae_on_valid_predictions ?? 0).toFixed(2)}; evidence quotes are exact source text in ${pct(A.evidence_quote_substring)}.`),
      h('div', { class: 'table-wrap' }, table(['Final label source', 'Topic', 'Intent', 'Severity exact', 'Severity ±1'], rows, [1, 2, 3, 4])),
      h('p', { class: 'dim', style: 'font-size:12.5px' }, 'Golden labels were never model inputs. Six Luna-labelled rows are too few for a separate accuracy claim.'),
      source(o.golden_evaluation.source_file)));
  }
  const ir = o.interrupt_resume?.value, st = o.system_tests?.value, gc = o.grouping_comparison?.value, cp = o.cost_pilot?.value;
  panels.push(h('div', { class: 'card' },
    h('h3', {}, 'Run controls and tests'),
    h('ul', { class: 'plain' },
      ir && h('li', {}, `Interrupt & resume: ${fmt.format(ir.before_completed_ids)} rows complete before the interruption, ${fmt.format(ir.after_completed_ids)} after resume; ${ir.resume_sent_before_ids} already-complete IDs re-sent `, check(ir.passed, 'pass', 'fail')),
      st && h('li', {}, 'Planted wrong label detected by the verifier ', check(st.planted_error_detected, 'yes', 'no'), ' · injected instructions ignored, IDs and quotes stayed grounded ',
        check(st.all_luna_ids_authoritative && st.all_luna_quotes_grounded && st.all_verifier_ids_authoritative && st.all_verifier_quotes_grounded, 'yes', 'no')),
      gc && h('li', {}, `Grouping: adopted ${gc.adopted.replace('_', ' ')} (membership-weighted coherent rate ${pct(gc.embedding_v2.membership_weighted_coherent_rate_overall)} vs ${pct(gc.tfidf_v1.membership_weighted_coherent_rate_overall)} for TF-IDF v1).`),
      cp && h('li', {}, `100-review pilot: cold ${cp.cold_wall_seconds.toFixed(1)} s, warm ${cp.warm_wall_seconds.toFixed(3)} s with ${cp.warm_new_enrichment_calls} new enrichment calls. All saved Luna calls ≈ $${cp.all_measured_api_equivalent_usd.toFixed(2)} at API rates (subscription-authenticated; estimate, not an invoice).`),
    ),
    source('evals/recovery/interrupt_resume_check.json · evals/system_tests/system_test_report.json · artifacts/full_run_v2/grouping_comparison.json · cost/report.json')));
  $('#quality').replaceChildren(...panels);
}

// ---------- issue drawer ----------
const drawer = $('#drawer'), backdrop = $('#backdrop');
function closeDrawer() { drawer.classList.remove('open'); backdrop.classList.remove('open'); drawer.setAttribute('aria-hidden', 'true'); }
backdrop.addEventListener('click', closeDrawer);
document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeDrawer(); });

function reviewItem(r) {
  return h('blockquote', { class: 'ev' }, quoted(r.review_text, r.evidence_quote),
    h('span', { class: 'who' }, `Review ${r.review_id} · ${r.intent} · severity ${r.severity}`, r.label_source ? ` · ${r.label_source === 'luna_teacher' ? 'Luna label' : 'local classifier'}` : ''));
}

async function openIssue(id) {
  drawer.replaceChildren(h('p', { class: 'dim' }, 'Loading…'));
  drawer.classList.add('open'); backdrop.classList.add('open'); drawer.setAttribute('aria-hidden', 'false');
  try {
    const d = await api(`issue?id=${encodeURIComponent(id)}&limit=10&order=sample`);
    const i = d.issue;
    const sevMax = Math.max(...d.severity_distribution.map((x) => x.n));
    const list = h('div', {}, d.members.map(reviewItem));
    let offset = d.members.length;
    const more = h('button', { class: 'btn', type: 'button', onclick: async () => {
      more.disabled = true;
      const next = await api(`issue?id=${encodeURIComponent(id)}&limit=10&order=sample&offset=${offset}`);
      list.append(...next.members.map(reviewItem));
      offset += next.members.length;
      more.disabled = next.members.length < 10;
    } }, 'Load more');
    drawer.replaceChildren(
      h('button', { class: 'btn close', type: 'button', onclick: closeDrawer }, 'Close'),
      h('div', { class: 'dim mono' }, i.issue_id),
      h('h2', {}, i.issue_name),
      h('p', {}, i.description),
      h('div', {}, h('span', { class: 'tag' }, i.topic), h('span', { class: 'tag' }, i.coherent ? 'coherent' : 'low coherence'), h('span', { class: 'tag' }, i.actionable ? 'actionable' : 'generic'), ...i.flags.map((f) => h('span', { class: 'tag' }, f))),
      h('div', { class: 'kv' },
        ...[['Rank', `#${i.rank}`], ['Complaints', fmt.format(i.complaint_count)], ['Mean severity', i.mean_severity], ['Priority score', fmt.format(i.priority_score)]]
          .map(([k, v]) => h('div', {}, h('div', { class: 'k' }, k), h('div', { class: 'v' }, v)))),
      h('h3', {}, 'Severity mix'),
      h('div', { class: 'table-wrap' }, table(['Severity', 'Reviews', ''], d.severity_distribution.map((x) => [x.severity, fmt.format(x.n),
        h('div', { class: 'inline-bar', style: 'min-width:140px' }, h('span', { style: `width:${(x.n / sevMax) * 100}%` }))]), [1])),
      d.memo_evidence.length ? h('h3', { style: 'margin-top:14px' }, 'Evidence given to the memo role') : null,
      ...d.memo_evidence.map((e) => h('blockquote', { class: 'ev' }, quoted(e.review_text, e.evidence_quote), h('span', { class: 'who' }, `Review ${e.review_id} · severity ${e.severity}`))),
      h('h3', { style: 'margin-top:14px' }, 'Member reviews'),
      h('p', { class: 'sub' }, 'Deterministic pseudo-random sample of this issue\'s members (ordered by MD5 of the review ID).'),
      list, more,
      h('p', { class: 'dim', style: 'font-size:12.5px' }, `Issue name and description: grouping role (${i.label_config.split(':')[0]}). Membership: code.`),
    );
  } catch (e) {
    drawer.replaceChildren(h('button', { class: 'btn close', type: 'button', onclick: closeDrawer }, 'Close'), h('p', { class: 'error' }, `Could not load ${id}: ${e.message}`));
  }
}

// ---------- review lookup ----------
$('#lookup').addEventListener('submit', async (e) => {
  e.preventDefault();
  const id = $('#lookup-id').value.trim();
  const out = $('#lookup-result');
  out.replaceChildren(h('p', { class: 'dim' }, 'Loading…'));
  try {
    const { review: r } = await api(`review?id=${encodeURIComponent(id)}`);
    const rows = [
      ['Status', r.status + (r.quarantine_reason ? ` (${r.quarantine_reason})` : '')],
      ['Topic / intent / severity', r.topic ? `${r.topic} / ${r.intent} / ${r.severity}` : '—'],
      ['Sentiment', r.sentiment ?? '—'],
      ['Needs review', r.needs_review == null ? '—' : String(r.needs_review)],
      ['Entities', r.entities ? JSON.stringify(r.entities) : '—'],
      ['Label source', r.label_source ? `${r.label_source} (${r.model})` : '—'],
      ['Exact-text reuse of', r.cache_source_id || '—'],
      ['Issue', r.issue_id ? h('button', { class: 'link-btn', type: 'button', onclick: () => openIssue(r.issue_id) }, `${r.issue_id} · ${r.issue_name}`) : '—'],
      ['Stars / likes / app version', `${r.review_rating ?? '—'} / ${r.review_likes ?? '—'} / ${r.app_version ?? '—'}`],
      ['Timestamp', r.review_timestamp ?? '—'],
      ['Source row SHA-256', h('code', {}, r.source_sha256)],
    ];
    out.replaceChildren(
      r.review_text ? h('blockquote', { class: 'ev' }, quoted(r.review_text, r.evidence_quote)) : h('p', { class: 'dim' }, '(empty review text)'),
      h('div', { class: 'table-wrap' }, table(['Field', 'Value'], rows)),
    );
  } catch (err) {
    out.replaceChildren(h('p', { class: 'error' }, `Not found or invalid ID (${err.message}).`));
  }
});

// ---------- table toggles & filters ----------
document.querySelectorAll('[data-toggle-table]').forEach((btn) => {
  btn.addEventListener('click', () => {
    const t = $(`#${btn.dataset.toggleTable}-table`);
    t.hidden = !t.hidden;
    btn.textContent = t.hidden ? 'Show table' : 'Hide table';
  });
});
document.querySelectorAll('[data-filter]').forEach((b) => b.addEventListener('click', () => {
  rankFilter = b.dataset.filter;
  document.querySelectorAll('[data-filter]').forEach((x) => x.setAttribute('aria-pressed', String(x === b)));
  renderRanking(openIssue);
}));
$('#topic-filter').addEventListener('change', () => renderRanking(openIssue));

// ---------- boot ----------
let topicsData, trendData;
function drawCharts() {
  if (topicsData) topicChart($('#topic-chart'), topicsData);
  if (trendData) { lineChart($('#trend-chart'), trendData); volumeChart($('#volume-chart'), trendData); }
}
let resizeTimer;
window.addEventListener('resize', () => { clearTimeout(resizeTimer); resizeTimer = setTimeout(drawCharts, 120); });

async function boot() {
  const jobs = [
    api('overview').then((o) => { renderTiles(o); renderQuality(o); }),
    api('recommendation').then((r) => renderReco(r, openIssue)),
    api('topics').then((t) => {
      topicsData = t.topics;
      $('#topic-table').replaceChildren(table(['Rank', 'Topic', 'Issues', 'Complaints', 'Severity sum', 'Mean severity', 'Priority score'],
        t.topics.map((x) => [x.rank, x.topic, x.issue_count, fmt.format(x.complaint_count), fmt.format(x.severity_sum), x.mean_severity, fmt.format(x.priority_score)]), [0, 2, 3, 4, 5, 6]));
      drawCharts();
    }),
    api('trend').then((t) => {
      trendData = t.months;
      $('#trend-table').replaceChildren(table(['Month', 'Reviews', 'Classified', 'Complaints + cancellations', 'Share'],
        t.months.map((m) => [m.month, fmt.format(m.source_rows), fmt.format(m.completed), fmt.format(m.complaint_or_cancellation), pct(m.complaint_share)]), [1, 2, 3, 4]));
      drawCharts();
    }),
    api('ranking').then((r) => {
      rankingData = r.issues;
      const sel = $('#topic-filter');
      [...new Set(r.issues.map((x) => x.topic))].sort().forEach((t) => sel.append(h('option', { value: t }, t)));
      $('#rank-sub').append(' ', h('span', {}, 'All rows equal the committed export grading/ranking.csv: '), check(r.all_match_saved_export, 'yes', 'no'));
      renderRanking(openIssue);
    }),
    api('health').then((x) => {
      $('#footer-health').textContent = `Database check ${new Date(x.checked_at).toISOString().slice(0, 19)}Z: ${fmt.format(x.reviews)} review rows, ${fmt.format(x.memberships)} issue memberships, ${x.issues} issues.`;
    }),
  ];
  const results = await Promise.allSettled(jobs);
  results.forEach((r) => { if (r.status === 'rejected') console.error(r.reason); });
  if (results.some((r) => r.status === 'rejected')) {
    document.querySelector('header.top').append(h('p', { class: 'error' }, 'Some data could not be loaded from the API. Refresh to retry.'));
  }
}
boot();
