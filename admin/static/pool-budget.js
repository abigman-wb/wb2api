/* 号池窗口预算（设置页卡片）：读写 /api/pool/window-budget，并展示每个号当前窗口已用积分。
 *
 * 为什么单独一个文件：index.html 已经 7000+ 行，面板逻辑按主题拆到 static/*.js
 * （ip-management.js / usage-ip.js / key-ip.js 都是这个做法），避免继续膨胀。
 * 依赖 index.html 里的全局 api() / safeJson() / $()，与其它面板脚本一致。
 */
window.WBPoolBudget = (() => {
  const $ = (sel) => document.querySelector(sel);
  const money = (n) => (Math.round(Number(n || 0) * 100) / 100).toLocaleString('zh-CN');

  function status(msg, bad) {
    const el = $('#wbBudgetStatus');
    if (!el) return;
    el.textContent = msg || '';
    el.style.color = bad ? 'var(--danger, #d33)' : 'var(--fg-muted)';
  }

  function renderAccounts(data) {
    const box = $('#wbBudgetAccounts');
    if (!box) return;
    const rows = data.accounts || [];
    if (!data.enabled) {
      box.innerHTML = '<div style="color:var(--fg-muted)">当前未启用（预算 = 0），号池不做窗口限制。</div>';
      return;
    }
    if (!rows.length) {
      box.innerHTML = '<div style="color:var(--fg-muted)">没有 active 账号。</div>';
      return;
    }
    const head = '<div style="display:grid;grid-template-columns:1fr 120px;gap:8px;color:var(--fg-muted);'
      + 'padding-bottom:4px;border-bottom:1px solid var(--border);margin-bottom:6px">'
      + `<span>账号（最近 ${data.hours} 小时窗口）</span><span style="text-align:right">已用 / 预算</span></div>`;
    const body = rows.map((a) => {
      const pct = a.percent == null ? 0 : Math.min(100, a.percent);
      const color = a.over ? '#d33' : (pct >= 80 ? '#d97706' : '#16a34a');
      return `<div style="display:grid;grid-template-columns:1fr 120px;gap:8px;align-items:center;padding:3px 0">
        <span>${a.name}${a.over ? ' <b style="color:#d33">· 已达预算，选号时会被跳过</b>' : ''}</span>
        <span style="text-align:right;font-variant-numeric:tabular-nums">${money(a.used)} / ${money(a.budget)}
          <span style="display:inline-block;width:56px;height:6px;background:var(--surface-2,#eee);border-radius:3px;vertical-align:middle;margin-left:6px">
            <span style="display:block;width:${pct}%;height:100%;background:${color};border-radius:3px"></span>
          </span>
        </span>
      </div>`;
    }).join('');
    box.innerHTML = head + body;
  }

  async function load() {
    try {
      const { ok, data } = await safeJson(await api('/api/pool/window-budget'));
      if (!ok) throw new Error(data.detail || '读取窗口预算失败');
      const budgetEl = $('#wbBudgetInput');
      const hoursEl = $('#wbBudgetHoursInput');
      if (budgetEl) budgetEl.value = data.budget;
      if (hoursEl) hoursEl.value = data.hours;
      const src = data.source === 'saved' ? '后台保存值'
        : data.source === 'env' ? '环境变量'
          : '未启用';
      status(`生效来源：${src}${data.source === 'env' ? '（保存后以后台值为准）' : ''}`);
      renderAccounts(data);
    } catch (e) {
      status(e.message || '读取失败', true);
    }
  }

  async function save() {
    const budget = Number($('#wbBudgetInput')?.value);
    const hours = Number($('#wbBudgetHoursInput')?.value);
    if (!Number.isFinite(budget) || budget < 0) return status('预算必须是不小于 0 的数字', true);
    if (!Number.isFinite(hours) || hours < 0.5 || hours > 24) return status('窗口长度必须在 0.5–24 小时之间', true);
    const btn = $('#wbBudgetSave');
    if (btn && btn.disabled) return;
    if (btn) btn.disabled = true;
    try {
      const { ok, data } = await safeJson(await api('/api/pool/window-budget', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ budget, hours }),
      }));
      if (!ok) throw new Error(data.detail || '保存失败');
      status(`已保存：${data.budget} 积分 / ${data.hours} 小时`);
      renderAccounts(data);
    } catch (e) {
      status(e.message || '保存失败', true);
    } finally {
      if (btn) btn.disabled = false;
    }
  }

  async function reset() {
    try {
      const { ok, data } = await safeJson(await api('/api/pool/window-budget', { method: 'DELETE' }));
      if (!ok) throw new Error(data.detail || '恢复默认失败');
      const budgetEl = $('#wbBudgetInput');
      const hoursEl = $('#wbBudgetHoursInput');
      if (budgetEl) budgetEl.value = data.budget;
      if (hoursEl) hoursEl.value = data.hours;
      status('已恢复默认（回落到环境变量）');
      renderAccounts(data);
    } catch (e) {
      status(e.message || '恢复默认失败', true);
    }
  }

  return { load, save, reset };
})();
