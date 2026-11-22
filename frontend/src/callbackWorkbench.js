import {api} from './store.js';
import {button, empty, escape, heading, panel, pill, time} from './shared.js';

const outcomes = {no_answer:'未接通', requested_callback:'约定回拨', reached:'已确认接通'};
const channels = {phone:'电话', in_person:'当面'};
const contactRoles = {primary:'第一联系人', backup:'备用联系人'};

function options(values) {
  return Object.entries(values).map(([id,label]) => `<option value="${escape(id)}">${escape(label)}</option>`).join('');
}

function badge(row) {
  if (row.claim_id) return pill(`已领取 · ${row.claimer_name}`, 'warn');
  return row.due ? pill('到点待领取', 'warn') : pill('未到期');
}

function claimForm(row) {
  return `<form data-do="claimCallback" data-id="${row.id}" data-appointment="${row.appointment}">${button('领取回拨')}</form>`;
}

function receiptForm(row) {
  return `<form class="stack ops-action" data-do="claimReceipt" data-id="${row.claim_id}">
    <div class="ops-fields"><label>联络方式<select name="channel">${options(channels)}</select></label>
    <label>联系结果<select name="outcome">${options(outcomes)}</select></label></div>
    <label>回执纪要<textarea name="note" rows="2" minlength="3" maxlength="600" required placeholder="记录本次回拨的沟通结果及下一步"></textarea></label>
    ${button('提交回执')}</form>
  <form data-do="releaseClaim" data-id="${row.claim_id}">${button('中断并释放','secondary')}</form>`;
}

function row(state, item) {
  const mine = item.claim_id && item.claimed_by === state.user.id;
  const isLead = state.user.role === 'lead';
  const latest = item.last_outcome ? `<p class="ops-copy">最新预约：${outcomes[item.last_outcome]} · ${escape(item.last_recorder)} · ${time(item.last_attempt_at)}<br>${escape(item.last_note)}</p>` : '';
  const claimLine = item.claim_id
    ? `<p class="ops-time">领取人 ${escape(item.claimer_name)} · ${time(item.claimed_at)} 领取 · ${time(item.claim_expires_at)} 超时</p>`
    : '<p class="ops-time">暂无领取人</p>';
  const actions = isLead && !item.claim_id && item.due ? claimForm(item) : mine ? receiptForm(item) : '';
  return `<article class="ops-item"><div class="ops-item-head"><div><h3>${escape(item.room)} · ${escape(item.resident_name)}</h3>
    <p><a class="text-link" href="#contacts/${item.alert_id}">联络事项 #${item.id}</a> · ${contactRoles[item.contact_role]}</p></div>
    <div class="data-line">${badge(item)}</div></div>
    <p class="ops-copy">${escape(item.purpose)}</p>${latest}
    <p class="ops-time">回拨到期 ${time(item.next_contact_at)}</p>${claimLine}${actions}</article>`;
}

export async function callbacksPage(state) {
  const {items, claim_minutes: claimMinutes} = await api('/callback-queue');
  const due = items.filter(item => item.due && !item.claim_id).length;
  const claimed = items.filter(item => item.claim_id).length;
  const rows = items.map(item => row(state, item)).join('');
  return `${heading('家属联络','回拨工作台','值班长领取到点回拨，在时限内登记回执；超时未登记自动恢复待领。')}
    <div class="ops-layout"><section class="paper">${panel('回拨队列', `${due} 项到点待领 · ${claimed} 项已领取 · 领取时限 ${claimMinutes} 分钟`)}
    ${rows || empty('暂无待回拨事项。家属未接通或约定回拨后会进入本队列。')}</section></div>`;
}

export const callbackActions = {
  claimCallback(form) {
    if (!form.dataset.claimKey) {
      form.dataset.claimKey = globalThis.crypto?.randomUUID?.() || `claim-${Date.now()}-${Math.random().toString(16).slice(2)}`;
    }
    return api(`/family-contacts/${form.dataset.id}/claims`, 'POST',
      {claim_key: form.dataset.claimKey, appointment: Number(form.dataset.appointment)}).then(() => '已领取，请在时限内登记回执');
  },
  claimReceipt(form, data) {
    return api(`/callback-claims/${form.dataset.id}/receipt`, 'POST', Object.fromEntries(data)).then(() => '回执已提交');
  },
  releaseClaim(form) {
    return api(`/callback-claims/${form.dataset.id}/release`, 'POST', {}).then(() => '已释放，回拨恢复待领');
  },
};
