from fastapi import HTTPException
from psycopg.errors import UniqueViolation

from .db import audit

# Demo callback policy: a claim leases the due callback for this many minutes.
CLAIM_MINUTES = 20
CALLBACK_MINUTES = {'no_answer': 30, 'requested_callback': 60}


def lock_contact(db, contact_id):
    contact = db.execute(
        'SELECT *, next_contact_at <= now() AS due FROM family_contacts WHERE id=%s FOR UPDATE',
        (contact_id,),
    ).fetchone()
    if not contact:
        raise HTTPException(404, '家属联络记录不存在')
    return contact


def sweep_expired(db, contact_id):
    """Mark timed-out claims expired. Callers hold the contact row lock first."""
    db.execute(
        '''UPDATE callback_claims SET status='expired', finished_at=now()
           WHERE contact_id=%s AND status='active' AND expires_at <= now()''',
        (contact_id,),
    )


def active_claim(db, contact_id):
    return db.execute(
        '''SELECT c.*, s.name AS claimer_name FROM callback_claims c JOIN staff s ON s.id=c.claimed_by
           WHERE c.contact_id=%s AND c.status='active' AND c.expires_at > now()''',
        (contact_id,),
    ).fetchone()


def apply_attempt(db, contact_id, actor, channel, outcome, note):
    """Record one contact attempt and move the appointment forward.

    Every recorded attempt bumps the appointment epoch, so claims and queue
    snapshots taken against the previous appointment become stale at once.
    """
    attempt = db.execute(
        '''INSERT INTO family_contact_attempts(contact_id,recorded_by,channel,outcome,note)
           VALUES (%s,%s,%s,%s,%s) RETURNING id''',
        (contact_id, actor, channel, outcome, note),
    ).fetchone()
    minutes = CALLBACK_MINUTES.get(outcome)
    result = db.execute(
        '''UPDATE family_contacts SET status=%s, next_contact_at=now() + make_interval(mins => %s),
                  appointment=appointment+1
           WHERE id=%s RETURNING *''',
        ('reached' if minutes is None else 'callback', minutes, contact_id),
    ).fetchone()
    return attempt['id'], result


def finish_claim(db, claim, status, attempt_id=None):
    db.execute(
        'UPDATE callback_claims SET status=%s, finished_at=now(), attempt_id=%s WHERE id=%s',
        (status, attempt_id, claim['id']),
    )


def claim_callback(db, contact_id, data, actor):
    contact = lock_contact(db, contact_id)
    sweep_expired(db, contact_id)
    replay = db.execute('SELECT * FROM callback_claims WHERE claim_key=%s', (data.claim_key,)).fetchone()
    if replay:
        if replay['contact_id'] != contact_id:
            raise HTTPException(409, '领取标识已用于其他回拨事项')
        return replay
    if contact['status'] != 'callback':
        raise HTTPException(409, '仅待回拨的联络事项可领取')
    if not contact['due']:
        raise HTTPException(409, '回拨时间未到，请到期后再领取')
    if data.appointment != contact['appointment']:
        raise HTTPException(409, '预约已更新，请刷新回拨队列')
    if active_claim(db, contact_id):
        raise HTTPException(409, '该回拨已被其他班组领取')
    try:
        row = db.execute(
            '''INSERT INTO callback_claims(contact_id,appointment,claim_key,claimed_by,expires_at)
               VALUES (%s,%s,%s,%s,now() + make_interval(mins => %s)) RETURNING *''',
            (contact_id, contact['appointment'], data.claim_key, actor, CLAIM_MINUTES),
        ).fetchone()
    except UniqueViolation as exc:
        raise HTTPException(409, '该回拨已被其他班组领取') from exc
    audit(db, actor, 'callback_claimed', 'callback_claim', row['id'],
          contact_id=contact_id, appointment=contact['appointment'])
    return row


def load_claim(db, claim_id):
    claim = db.execute('SELECT * FROM callback_claims WHERE id=%s', (claim_id,)).fetchone()
    if not claim:
        raise HTTPException(404, '领取记录不存在')
    return claim


def claim_receipt(db, claim_id, data, actor):
    claim = load_claim(db, claim_id)
    contact = lock_contact(db, claim['contact_id'])
    sweep_expired(db, contact['id'])
    current = db.execute('SELECT * FROM callback_claims WHERE id=%s', (claim_id,)).fetchone()
    if current['claimed_by'] != actor:
        raise HTTPException(403, '仅领取人可提交本次回执')
    if current['status'] != 'active':
        raise HTTPException(409, '领取已失效，请刷新回拨队列')
    if current['appointment'] != contact['appointment']:
        raise HTTPException(409, '预约已更新，请刷新回拨队列')
    if contact['status'] == 'reached':
        raise HTTPException(409, '联络事项已确认接通')
    attempt_id, result = apply_attempt(db, contact['id'], actor, data.channel, data.outcome, data.note)
    finish_claim(db, current, 'done', attempt_id)
    audit(db, actor, 'callback_receipt_recorded', 'callback_claim', claim_id,
          contact_id=contact['id'], attempt_id=attempt_id, outcome=data.outcome)
    return result


def release_claim(db, claim_id, actor):
    claim = load_claim(db, claim_id)
    contact = lock_contact(db, claim['contact_id'])
    sweep_expired(db, contact['id'])
    current = db.execute('SELECT * FROM callback_claims WHERE id=%s', (claim_id,)).fetchone()
    if current['claimed_by'] != actor:
        raise HTTPException(403, '仅领取人可释放本次领取')
    if current['status'] != 'active':
        raise HTTPException(409, '领取已结束，请刷新回拨队列')
    finish_claim(db, current, 'released')
    audit(db, actor, 'callback_claim_released', 'callback_claim', claim_id, contact_id=contact['id'])
    return db.execute('SELECT * FROM callback_claims WHERE id=%s', (claim_id,)).fetchone()
