from fastapi import APIRouter, Depends

from . import callbacks
from .auth import current_staff, require
from .callbacks_schemas import ClaimCallback, ClaimReceipt
from .db import connect
from .schemas import Role

router = APIRouter(tags=['callback workbench'])
lead = require(Role.LEAD)


@router.get('/callback-queue')
def callback_queue(staff=Depends(current_staff)):
    with connect() as db:
        items = db.execute(
            '''SELECT c.id, c.alert_id, c.purpose, c.contact_role, c.status, c.appointment, c.next_contact_at,
                      c.next_contact_at <= now() AS due, r.name AS resident_name, r.room,
                      la.outcome AS last_outcome, la.note AS last_note, la.attempted_at AS last_attempt_at,
                      ls.name AS last_recorder,
                      cl.id AS claim_id, cl.claimed_by, cl.claimed_at, cl.expires_at AS claim_expires_at,
                      cs.name AS claimer_name
               FROM family_contacts c
               JOIN alerts a ON a.id=c.alert_id JOIN residents r ON r.id=a.resident_id
               LEFT JOIN LATERAL (
                   SELECT x.outcome, x.note, x.attempted_at, x.recorded_by FROM family_contact_attempts x
                   WHERE x.contact_id=c.id ORDER BY x.id DESC LIMIT 1
               ) la ON true LEFT JOIN staff ls ON ls.id=la.recorded_by
               LEFT JOIN LATERAL (
                   SELECT y.id, y.claimed_by, y.claimed_at, y.expires_at FROM callback_claims y
                   WHERE y.contact_id=c.id AND y.status='active' AND y.expires_at > now()
                   ORDER BY y.id DESC LIMIT 1
               ) cl ON true LEFT JOIN staff cs ON cs.id=cl.claimed_by
               WHERE c.status='callback'
               ORDER BY c.next_contact_at, c.id LIMIT 150''',
        ).fetchall()
        return {'items': items, 'claim_minutes': callbacks.CLAIM_MINUTES}


@router.post('/family-contacts/{contact_id}/claims', status_code=201)
def new_claim(contact_id: int, data: ClaimCallback, staff=Depends(lead)):
    with connect() as db:
        return callbacks.claim_callback(db, contact_id, data, staff['id'])


@router.post('/callback-claims/{claim_id}/receipt', status_code=201)
def receipt(claim_id: int, data: ClaimReceipt, staff=Depends(lead)):
    with connect() as db:
        return callbacks.claim_receipt(db, claim_id, data, staff['id'])


@router.post('/callback-claims/{claim_id}/release')
def release(claim_id: int, staff=Depends(lead)):
    with connect() as db:
        return callbacks.release_claim(db, claim_id, staff['id'])
