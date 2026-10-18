const DATA_URLS = {
  strategy: './data/strategy.json',
  performance: './data/performance.json',
  fit: './data/fit_curve.json',
};
const bondList = document.querySelector('#bond-list');
const toast = document.querySelector('#toast');
let toastTimer;

const money = value => Number(value).toFixed(2);
const pct = (value, digits = 2) => `${Number(value).toFixed(digits)}%`;
const escapeHTML = value => String(value).replace(/[&<>"']/g, char => ({
  '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
}[char]));

function showToast(message) {
  toast.textContent = message;
  toast.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toast.classList.remove('show'), message.length > 35 ? 6000 : 2300);
}

function renderBonds(bonds) {
  if (!bonds.length) {
    bondList.innerHTML = '<div class="empty-state">本期没有同时满足全部入场门槛的候选。请等待新的月度信号。</div>';
    return;
  }
  bondList.innerHTML = bonds.map((bond, index) => {
    const deviation = Number(bond.deviation);
    const isExpensive = deviation > 0;
    return `<article class="bond-card">
      <span class="rank-number">${String(index + 1).padStart(2, '0')}</span>
      <div class="bond-main">
        <div class="bond-name-line"><strong class="bond-name">${escapeHTML(bond.name)}</strong><span class="bond-tag"><i></i>通过风控</span></div>
        <div class="bond-code">${escapeHTML(bond.code)} · 正股 ${money(bond.underlying)}</div>
      </div>
      <div class="bond-value"><div class="deviation-label">偏离度</div><div class="deviation-value${isExpensive ? ' high' : ''}">${deviation > 0 ? '+' : ''}${pct(deviation, 2)}</div></div>
      <div class="price-block"><div class="bond-price">${money(bond.price)}</div><div class="bond-ytm">YTM ${pct(bond.ytm, 2)}</div></div>
    </article>`;
  }).join('');
}

function renderStrategy(data) {
  document.querySelector('#as-of-date').textContent = data.asOf;
  document.querySelector('#candidate-count').textContent = String(data.bonds.length).padStart(2, '0');
  document.querySelector('#annualized-return').textContent = pct(data.backtest.annualizedReturnPct);
  document.querySelector('#max-drawdown').textContent = pct(data.backtest.maxDrawdownPct);
  document.querySelector('#sharpe-ratio').textContent = Number(data.backtest.sharpe).toFixed(2);
  document.querySelector('#backtest-period').textContent = `${data.backtest.periodStart} — ${data.backtest.periodEnd}`;
  renderBonds(data.bonds);
}

function renderFreshness(data, offline = false, stored = false) {
  const ageDays = (Date.now() - Date.parse(`${data.asOf}T00:00:00+08:00`)) / 86400000;
  const stale = ageDays > 14;
  document.querySelector('#snapshot-state').textContent = offline || stored ? '离线快照' : stale ? '历史快照' : '收盘快照';
  const status = document.querySelector('#update-status');
  status.classList.toggle('warning', offline || stored || stale);
  status.textContent = stored ? '网络载入失败，正在显示上一次成功保存的数据。'
    : offline ? '当前使用离线快照；联网后点右上角刷新。'
    : stale ? '行情日期已超过 14 天，请查看 GitHub Actions 的更新状态。'
    : '行情收盘后自动更新 · 调仓按月执行 · 历史回测保留所示区间';
}

function renderPerformance(data) {
  const series = data.series || [];
  if (series.length < 2) throw new Error('Not enough performance history');

  const width = 390;
  const height = 218;
  const margin = { top: 12, right: 9, bottom: 30, left: 42 };
  const plotWidth = width - margin.left - margin.right;
  const plotHeight = height - margin.top - margin.bottom;
  const keys = ['strategy', 'sp500', 'dividendLow'];
  const colors = { strategy: '#94efcb', sp500: '#7fb9ff', dividendLow: '#f6bf75' };
  const values = series.flatMap(point => keys.map(key => Number(point[key])));
  const yMin = Math.min(80, Math.floor(Math.min(...values) / 50) * 50);
  const yMax = Math.ceil(Math.max(...values) / 100) * 100;
  const x = index => margin.left + (index / (series.length - 1)) * plotWidth;
  const y = value => margin.top + ((yMax - value) / (yMax - yMin)) * plotHeight;
  const tickStep = yMax - yMin > 350 ? 100 : 50;
  const ticks = [];
  for (let value = Math.ceil(yMin / tickStep) * tickStep; value <= yMax; value += tickStep) ticks.push(value);
  const horizontalGrid = ticks.map(value => `
    <line class="chart-gridline" x1="${margin.left}" y1="${y(value)}" x2="${width - margin.right}" y2="${y(value)}" />
    <text class="axis-label" x="${margin.left - 7}" y="${y(value) + 3}" text-anchor="end">${value}</text>`).join('');
  const xIndexes = [...new Set([0, Math.floor((series.length - 1) / 2), series.length - 1])];
  const xLabels = xIndexes.map(index => `<text class="axis-label" x="${x(index)}" y="${height - 7}" text-anchor="${index === 0 ? 'start' : index === series.length - 1 ? 'end' : 'middle'}">${escapeHTML(series[index].date.slice(0, 4))}</text>`).join('');
  const paths = keys.map(key => {
    const points = series.map((point, index) => `${index === 0 ? 'M' : 'L'}${x(index).toFixed(2)},${y(Number(point[key])).toFixed(2)}`).join(' ');
    const label = data.labels[key] || key;
    return `<path class="series-line" d="${points}" stroke="${colors[key]}"><title>${escapeHTML(label)}：起点 100，最新 ${Number(series.at(-1)[key]).toFixed(1)}</title></path>`;
  }).join('');
  document.querySelector('#chart-period').textContent = `${data.startDate.slice(0, 4)} — ${data.endDate.slice(0, 4)}`;
  document.querySelector('#performance-legend').innerHTML = keys.map(key => `<span><i class="legend-dot" style="--legend-color:${colors[key]}"></i>${escapeHTML(data.labels[key] || key)}</span>`).join('');
  document.querySelector('#performance-chart').innerHTML = `<svg class="line-chart" viewBox="0 0 ${width} ${height}" aria-hidden="true" focusable="false">
    ${horizontalGrid}
    <line class="baseline" x1="${margin.left}" y1="${y(100)}" x2="${width - margin.right}" y2="${y(100)}" />
    ${paths}${xLabels}
  </svg>`;
  document.querySelector('#performance-returns').innerHTML = keys.map(key => {
    const gain = Number(series.at(-1)[key]) - Number(data.base || 100);
    return `<div class="return-item"><i class="legend-dot" style="--legend-color:${colors[key]}"></i><span>${escapeHTML((data.labels[key] || key).replace(/（.*?）/g, ''))}</span><strong>${gain >= 0 ? '+' : ''}${gain.toFixed(1)}%</strong></div>`;
  }).join('');
  document.querySelector('#performance-method').textContent = data.method;
}

function renderFitCurve(data) {
  const points = data.points || [];
  const curve = data.curve || [];
  if (!points.length || !curve.length) throw new Error('No full market valuation data');

  const width = 390;
  const height = 238;
  const margin = { top: 13, right: 10, bottom: 33, left: 43 };
  const plotWidth = width - margin.left - margin.right;
  const plotHeight = height - margin.top - margin.bottom;
  const xMax = Number(data.xMax || 600);
  const yMin = Number(data.yMinPct ?? 0);
  const yMax = Number(data.yMaxPct || 200);
  const x = value => margin.left + (Number(value) / xMax) * plotWidth;
  const y = value => margin.top + ((yMax - Number(value)) / (yMax - yMin)) * plotHeight;
  const xStep = xMax > 400 ? 100 : 50;
  const yStep = yMax > 180 ? 50 : 25;
  const grid = [];
  for (let value = 0; value <= xMax; value += xStep) {
    grid.push(`<line class="chart-gridline" x1="${x(value)}" y1="${margin.top}" x2="${x(value)}" y2="${height - margin.bottom}" />
      <text class="axis-label" x="${x(value)}" y="${height - 15}" text-anchor="middle">${value}</text>`);
  }
  for (let value = Math.ceil(yMin / yStep) * yStep; value <= yMax; value += yStep) {
    grid.push(`<line class="chart-gridline" x1="${margin.left}" y1="${y(value)}" x2="${width - margin.right}" y2="${y(value)}" />
      <text class="axis-label" x="${margin.left - 7}" y="${y(value) + 3}" text-anchor="end">${value}</text>`);
  }
  const dots = points.map(point => {
    const actual = Number(point.actual);
    const plotted = Math.min(yMax, Math.max(yMin, actual));
    const clipped = actual > yMax || actual < yMin;
    const title = `${point.code} · 转股价值 ${Number(point.x).toFixed(1)} 元 · 实际溢价 ${Number(point.actual).toFixed(1)}% · 模型合理 ${Number(point.fair).toFixed(1)}% · 偏离 ${Number(point.deviation) > 0 ? '+' : ''}${Number(point.deviation).toFixed(1)}%`;
    if (clipped) {
      const clippedAbove = actual > yMax;
      const edge = clippedAbove ? margin.top + 1 : margin.top + plotHeight - 1;
      const triangle = clippedAbove ? `M${x(point.x)},${edge} l-4,7 h8 z` : `M${x(point.x)},${edge} l-4,-7 h8 z`;
      return `<path class="scatter-dot clipped" d="${triangle}" data-tooltip="${escapeHTML(title)}"><title>${escapeHTML(title)}（超出纵轴范围，边缘标记显示）</title></path>`;
    }
    return `<circle class="scatter-dot" cx="${x(point.x)}" cy="${y(plotted)}" r="2.6" data-tooltip="${escapeHTML(title)}"><title>${escapeHTML(title)}</title></circle>`;
  }).join('');
  const curvePath = curve.map((point, index) => `${index === 0 ? 'M' : 'L'}${x(point.x).toFixed(2)},${y(point.fair).toFixed(2)}`).join(' ');
  const curvePoints = curve.map(point => `<circle class="curve-knot" cx="${x(point.x)}" cy="${y(point.fair)}" r="2"><title>转股价值约 ${Number(point.x).toFixed(0)} 元（区间 ${escapeHTML(point.range)}），模型合理溢价中位数 ${Number(point.fair).toFixed(1)}%，样本 ${point.count} 只</title></circle>`).join('');
  const clippedCount = Number(data.clippedActualCount || 0) + Number(data.belowRangeCount || 0);
  document.querySelector('#fit-as-of').textContent = `截至 ${data.asOf}`;
  document.querySelector('#fit-stats').innerHTML = `<span><strong>${Number(data.totalActive).toLocaleString('zh-CN')}</strong> 只在市样本</span><span>转股价值分箱宽度 ${data.binWidth} 元</span>`;
  document.querySelector('#fit-chart').innerHTML = `<svg class="fit-chart" viewBox="0 0 ${width} ${height}" aria-hidden="true" focusable="false">
    ${grid.join('')}
    <text class="axis-title" x="${margin.left + plotWidth / 2}" y="${height - 1}" text-anchor="middle">转股价值（元）</text>
    <text class="axis-title" transform="translate(11 ${margin.top + plotHeight / 2}) rotate(-90)" text-anchor="middle">转股溢价率（%）</text>
    ${dots}<path class="fit-line" d="${curvePath}"><title>各转股价值区间内模型合理溢价率的中位数</title></path>${curvePoints}
  </svg>`;
  document.querySelector('#fit-method').textContent = `散点为${data.totalActive}只当日有有效报价的在市转债，未套用策略买入风控门槛；模型合理溢价先逐券估算，再按转股价值每 ${data.binWidth} 元分箱取中位数形成曲线。偏离度 = 实际溢价 − 模型合理溢价。纵轴显示 ${yMin}% 至 ${yMax}%，${clippedCount} 只超出范围的样本以边缘标记显示，点按或悬停可查看原值。`;
}

function setTab(tab) {
  const rules = document.querySelector('#rules-panel');
  const candidates = document.querySelector('#candidates-title').closest('section');
  const overviewSections = [
    document.querySelector('.hero-card'),
    document.querySelector('.metrics-section'),
    ...document.querySelectorAll('.chart-section'),
  ];
  rules.hidden = tab !== 'rules';
  candidates.hidden = tab === 'rules';
  overviewSections.forEach(section => { section.hidden = tab === 'watchlist'; });
  document.querySelectorAll('.nav-item').forEach(button => {
    const selected = button.dataset.tab === tab;
    button.classList.toggle('active', selected);
    if (selected) button.setAttribute('aria-current', 'page');
    else button.removeAttribute('aria-current');
  });
  window.scrollTo({ top: 0, behavior: 'smooth' });
}

async function loadAllData() {
  const entries = await Promise.all(Object.entries(DATA_URLS).map(async ([key, url]) => {
    const response = await fetch(`${url}?v=${Date.now()}`, { cache: 'no-store' });
    if (!response.ok) throw new Error(`${key} snapshot unavailable`);
    return [key, { payload: await response.json(), offline: response.headers.get('X-CB-Offline') === '1' }];
  }));
  const results = Object.fromEntries(entries);
  const data = Object.fromEntries(entries.map(([key, value]) => [key, value.payload]));
  if (data.strategy.snapshotId && data.strategy.snapshotId !== data.fit.snapshotId) throw new Error('Snapshot versions differ');
  if (data.strategy.asOf !== data.fit.asOf) throw new Error('Snapshot dates differ');
  data.offline = Object.values(results).some(result => result.offline);
  return data;
}

function renderAll(data, stored = false) {
  renderStrategy(data.strategy);
  renderPerformance(data.performance);
  renderFitCurve(data.fit);
  renderFreshness(data.strategy, data.offline, stored);
}

async function refreshData(silent = false) {
  const button = document.querySelector('#refresh-button');
  button.classList.add('is-refreshing');
  try {
    const data = await loadAllData();
    renderAll(data);
    try { localStorage.setItem('cb-monthly-last-good', JSON.stringify(data)); } catch {}
    if (!silent) showToast(data.offline ? '当前显示离线快照' : `行情已同步，截至 ${data.strategy.asOf}`);
  } catch {
    let saved;
    try { saved = JSON.parse(localStorage.getItem('cb-monthly-last-good')); } catch {}
    if (saved) {
      renderAll(saved, true);
    } else {
      document.querySelector('#update-status').textContent = '首次载入失败，请联网后点击右上角刷新。';
      document.querySelector('#update-status').classList.add('warning');
    }
    if (!silent) showToast('数据载入失败，已保留上次快照');
  } finally {
    button.classList.remove('is-refreshing');
  }
}

document.querySelectorAll('[data-tab]').forEach(button => {
  button.addEventListener('click', () => setTab(button.dataset.tab));
});
document.querySelector('#refresh-button').addEventListener('click', () => refreshData());
document.querySelector('[data-tab="watchlist"].text-button').addEventListener('click', () => setTab('watchlist'));
document.querySelector('#fit-chart').addEventListener('click', event => {
  const point = event.target.closest('[data-tooltip]');
  if (point) showToast(point.dataset.tooltip);
});

refreshData(true).catch(() => showToast('数据载入失败，请检查网络后刷新'));

if ('serviceWorker' in navigator && location.protocol.startsWith('http')) {
  let reloading = false;
  navigator.serviceWorker.addEventListener('controllerchange', () => {
    if (!reloading) { reloading = true; location.reload(); }
  });
  window.addEventListener('load', () => navigator.serviceWorker.register('./sw.js', { updateViaCache: 'none' }).catch(() => {}));
}
