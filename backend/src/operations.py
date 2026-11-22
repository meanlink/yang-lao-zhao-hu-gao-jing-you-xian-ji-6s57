from uuid import uuid4

from fastapi import HTTPException

from . import callbacks, checklist, coverage, workflow
from .db import audit
from .schemas import AlertStatus, NewObservation

# Demo operations policy. A created task retains the SLA of the confirmed alert version.
RESPONSE_MINUTES = {3: 15, 2: 30, 1: 60}
ACTIVE_RESPONSES = ('assigned', 'accepted')


def create_response(db, alert_id, data, actor):
    alert = workflow.lock_alert(db, alert_id)
    if alert['status'] != AlertStatus.CONFIRMED or alert['revision'] != data.revision:
        raise HTTPException(409, '请刷新并选择已复核的当前告警版本')
    if workflow.active_response(db, alert_id):
        raise HTTPException(409, '本告警已有一条在办响应任务')
    minutes = RESPONSE_MINUTES[alert['priority']]
    unit = db.execute('SELECT unit FROM residents WHERE id=%s', (alert['resident_id'],)).fetchone()['unit']
    coverage.reserve(db, unit, data.assigned_to, data.coverage_id, minutes)
    row = db.execute(
        '''INSERT INTO care_responses(alert_id, alert_revision, priority, assigned_to, directed_by, instruction, coverage_id, due_at)
           VALUES (%s,%s,%s,%s,%s,%s,%s,now() + make_interval(mins => %s)) RETURNING *''',
        (alert_id, alert['revision'], alert['priority'], data.assigned_to, actor, data.instruction, data.coverage_id, minutes),
    ).fetchone()
    audit(db, actor, 'care_response_assigned', 'care_response', row['id'], alert_id=alert_id, sla_minutes=minutes, coverage_id=data.coverage_id)
    return row


def transition_response(db, response_id, action, actor, note=None):
    row = workflow.lock_response(db, response_id)
    if action != 'cancel' and row['assigned_to'] != actor:
        raise HTTPException(403, '仅指定照护员可接单或填写处置结果')
    allowed = {'accept': ('assigned',), 'complete': ('accepted',), 'cancel': ACTIVE_RESPONSES}
    if row['status'] not in allowed[action]:
        raise HTTPException(409, '任务状态已经变化，请刷新队列')
    if action == 'accept':
        result = db.execute(
            "UPDATE care_responses SET status='accepted',accepted_at=now() WHERE id=%s RETURNING *",
            (response_id,),
        ).fetchone()
    else:
        if action == 'complete':
            checklist.ensure_complete(db, row)
        status = 'completed' if action == 'complete' else 'cancelled'
        result = db.execute(
            'UPDATE care_responses SET status=%s,finished_at=now(),finish_note=%s WHERE id=%s RETURNING *',
            (status, note, response_id),
        ).fetchone()
    audit(db, actor, f'care_response_{result["status"]}', 'care_response', response_id, alert_id=row['alert_id'])
    return result


def open_contact(db, alert_id, data, actor):
    alert = workflow.lock_alert(db, alert_id)
    if alert['status'] not in (AlertStatus.CONFIRMED, AlertStatus.HANDOVER, AlertStatus.CLOSED):
        raise HTTPException(409, '告警经值班长复核后才能登记家属联络')
    row = db.execute(
        '''INSERT INTO family_contacts(alert_id,purpose,contact_role,opened_by) VALUES (%s,%s,%s,%s)
           ON CONFLICT (alert_id) DO NOTHING RETURNING *''',
        (alert_id, data.purpose, data.contact_role, actor),
    ).fetchone()
    if not row:
        raise HTTPException(409, '本条告警已有联络记录，请追加联系结果')
    audit(db, actor, 'family_contact_opened', 'family_contact', row['id'], alert_id=alert_id)
    return row


def record_attempt(db, contact_id, data, staff):
    contact = callbacks.lock_contact(db, contact_id)
    if contact['status'] == 'reached':
        raise HTTPException(409, '联络事项已确认接通')
    callbacks.sweep_expired(db, contact_id)
    claim = callbacks.active_claim(db, contact_id)
    if claim and claim['claimed_by'] != staff['id'] and staff['role'] != 'lead':
        raise HTTPException(409, '该回拨已由值班长领取，请待领取超时后再登记')
    latest = db.execute('SELECT COALESCE(max(id),0) AS id FROM family_contact_attempts WHERE contact_id=%s', (contact_id,)).fetchone()
    if latest['id'] != data.expected_attempt_id:
        raise HTTPException(409, '已登记较新的联系记录，请刷新后再追加')
    attempt_id, result = callbacks.apply_attempt(db, contact_id, staff['id'], data.channel, data.outcome, data.note)
    if claim:
        # The claim owner finishing directly closes the claim; a lead's direct
        # registration supersedes it so the old appointment leaves the pool at once.
        callbacks.finish_claim(db, claim, 'done' if claim['claimed_by'] == staff['id'] else 'superseded', attempt_id)
    audit(db, staff['id'], 'family_contact_attempt_recorded', 'family_contact', contact_id,
          attempt_id=attempt_id, outcome=data.outcome)
    return result


def schedule_followup(db, handover_id, data, actor):
    handover = db.execute('SELECT status FROM handovers WHERE id=%s', (handover_id,)).fetchone()
    if not handover:
        raise HTTPException(404, '交班记录不存在')
    if handover['status'] != 'signed':
        raise HTTPException(409, '请先签收当前交班再安排复查')
    row = db.execute(
        '''INSERT INTO handover_followups(handover_id,scheduled_by,checklist,due_at)
           VALUES (%s,%s,%s,now() + make_interval(mins => %s))
           ON CONFLICT (handover_id) DO NOTHING RETURNING *''',
        (handover_id, actor, data.checklist, data.delay_minutes),
    ).fetchone()
    if not row:
        raise HTTPException(409, '该交班已经安排了接班复查')
    audit(db, actor, 'handover_followup_scheduled', 'handover_followup', row['id'], handover_id=handover_id)
    return row


def finish_followup(db, followup_id, data, actor):
    row = db.execute(
        '''SELECT f.*,a.resident_id FROM handover_followups f JOIN handovers h ON h.id=f.handover_id
           JOIN alerts a ON a.id=h.alert_id WHERE f.id=%s FOR UPDATE OF f''',
        (followup_id,),
    ).fetchone()
    if not row:
        raise HTTPException(404, '接班复查不存在')
    if row['status'] != 'scheduled':
        raise HTTPException(409, '该复查已经提交结果')
    observation_id = None
    if data.result == 'needs_attention':
        data_note = f'接班复查 #{followup_id}：{data.note}'[:600]
        observation = workflow.enter_observation(db, NewObservation(
            ref=f'OBS-FU-{followup_id}-{uuid4().hex[:8].upper()}',
            resident_id=row['resident_id'], author_note=data_note,
        ), actor)
        observation_id = observation['id']
    result = db.execute(
        '''UPDATE handover_followups SET status=%s,result_note=%s,completed_by=%s,
                  completed_at=now(),observation_id=%s WHERE id=%s RETURNING *''',
        ('referred' if observation_id else 'stable', data.note, actor, observation_id, followup_id),
    ).fetchone()
    audit(db, actor, 'handover_followup_completed', 'handover_followup', followup_id,
          result=data.result, observation_id=observation_id)
    return result
