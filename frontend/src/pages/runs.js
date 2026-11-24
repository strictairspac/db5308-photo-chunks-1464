import {heading, panel} from '../components/common/heading.js';
import {records} from '../components/common/records.js';
import {badge} from '../components/common/status.js';
import {actionLabels, requestLabels} from '../constants/workflow.js';
import {topic} from '../constants/project.js';
import {date, esc} from '../utils/dom.js';
import {submit} from '../hooks/submit.js';

function requestDetail(row) {
  const notice = row.request_status==='withdrawn' ? '<p class="note">该申请已撤回，不能领取报告。</p>'
    : row.evidence_id ? `<a class="button" href="#evidence/${row.evidence_id}">查看业务证据</a>`
    : row.status==='failed' ? `<p class="error">${esc(row.error || '执行失败')}</p>`
    : '<p class="note">报告尚未出具，请稍后刷新。</p>';
  return `<dl><dt>申请号</dt><dd>${esc(row.slug)}</dd><dt>申请状态</dt><dd>${esc(requestLabels[row.request_status]||row.request_status)}</dd>
    <dt>执行状态</dt><dd>${badge(row.status)}</dd><dt>经办</dt><dd>${esc(row.owner)}</dd><dt>提交时间</dt><dd>${esc(date(row.created_at))}</dd></dl>${notice}`;
}

export function runsPage(state, identity) {
  const selected = state.runs.find(row=>row.id===identity) || state.runs[0];
  const cases = new Map(state.cases.map(row=>[row.id, row.title]));
  const rows = state.runs.map(row=>[`<a href="#runs/${row.id}">${esc(cases.get(row.case_id) || row.case_id)}</a>`, badge(row.status), esc(row.owner), esc(date(row.created_at))]);
  const result = state.evidence.find(row=>row.run_id===selected?.id);
  const active = selected && ['queued','running'].includes(selected.status);
  const operator = state.session.role==='operator';
  const applications = state.requests.filter(row=>row.run_id===selected?.id);
  const applicationRows = applications.map(row=>[esc(row.slug), esc(row.owner), esc(requestLabels[row.status]||row.status), esc(date(row.created_at)),
    operator && row.status==='active' ? `<button class="secondary" type="button" data-withdraw="${esc(row.slug)}">撤回</button>` : '']);
  const detail = selected ? `<h2>${esc(cases.get(selected.case_id) || selected.case_id)}</h2>
    <p data-testid="run-status">${badge(selected.status)}</p>
    <dl><dt>启动时间</dt><dd>${esc(date(selected.started_at))}</dd><dt>完成时间</dt><dd>${esc(date(selected.finished_at))}</dd></dl>
    ${selected.error ? `<p class="error">${esc(selected.error)}</p>` : ''}
    ${active?'<p class="note">后台处理中，记录会自动刷新。</p>':''}
    ${result?`<a class="button" href="#evidence/${result.id}">查看业务证据</a>`:''}` : '<h2>执行记录</h2><p>从档案创建一条执行记录后，可在此查看进度。</p>';
  const audit = state.audit.filter(x=>selected && x.reference===String(selected.id) && ['enqueue', 'complete', 'failed', 'void_run'].includes(x.action));
  return `${heading(topic.modules[2], `${state.spec.entity_labels[1]}与后台处理进度`, '<button class="secondary" type="button" id="refresh">刷新记录</button>')}
    <div class="spread"><section>${records(['档案','状态','经办','提交时间'], rows, '暂无执行记录。')}</section><aside class="brief">${detail}</aside></div>
    ${selected?panel('查验申请', records(['申请号','经办','申请状态','提交时间','操作'], applicationRows, '该执行暂无关联申请。')):''}
    ${panel('按申请号查询', '<form id="lookup-form"><label>申请号<input name="slug" required maxlength="48" placeholder="班组持有的申请号"></label><button>查询</button></form><div id="lookup-result"></div>')}
    ${audit.length?panel('操作记录', records(['操作者','操作','时间'], audit.map(x=>[esc(x.actor),esc(actionLabels[x.action]||x.action),esc(date(x.created_at))]))):''}`;
}

export function bindRuns(state, api, update, fail) {
  document.getElementById('refresh')?.addEventListener('click', update);
  document.querySelectorAll('[data-withdraw]').forEach(button=>button.addEventListener('click', async()=>{
    try { await api(`/requests/${encodeURIComponent(button.dataset.withdraw)}/withdraw`, {method:'POST'}); await update(); }
    catch (error) { fail(error); }
  }));
  submit('lookup-form', async data=>{
    const row = await api(`/requests/${encodeURIComponent(String(data.get('slug')).trim())}`);
    const box = document.getElementById('lookup-result');
    if (box) box.innerHTML = requestDetail(row);
  }, fail);
}
