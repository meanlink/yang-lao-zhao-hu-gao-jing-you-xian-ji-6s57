"""回拨领取工作台的验收测试。

运行前需要可用的 PostgreSQL（PGHOST/PGPORT/PGDATABASE/PGUSER/PGPASSWORD 环境变量），
测试会迁移并清空该库中的业务表。Redis 不参与这些用例。
"""
import os
import threading
import uuid

import pytest

psycopg = pytest.importorskip('psycopg')

try:
    psycopg.connect('', connect_timeout=2).close()
except Exception:
    pytest.skip('需要可用的 PostgreSQL（PG* 环境变量）', allow_module_level=True)

os.environ.setdefault('SEED_DEMO', 'false')

from fastapi.testclient import TestClient

from src import main
from src.auth import token_for
from src.db import connect, migrate

TABLES = ('audit_events', 'callback_claims', 'family_contact_attempts', 'family_contacts',
          'response_check_events', 'response_check_items', 'response_checklists', 'care_responses',
          'handover_followups', 'handovers', 'observations', 'alerts', 'shift_coverage',
          'residents', 'staff')


@pytest.fixture()
def client():
    migrate()
    with connect() as db:
        db.execute(f"TRUNCATE {','.join(TABLES)} RESTART IDENTITY CASCADE")
    with TestClient(main.app) as test_client:
        yield test_client


def auth(staff_id):
    return {'Authorization': f"Bearer {token_for({'id': staff_id})}"}


def bootstrap(client, suffix=''):
    """两班值班长 + 一名照护员，一条已复核告警及其到期回拨。"""
    with connect() as db:
        lead_a = db.execute("INSERT INTO staff(email,name,password_hash,role) VALUES "
                            "(%s,'甲班值班长','x','lead') RETURNING id", (f'lead-a{suffix}@test.local',)).fetchone()['id']
        lead_b = db.execute("INSERT INTO staff(email,name,password_hash,role) VALUES "
                            "(%s,'乙班值班长','x','lead') RETURNING id", (f'lead-b{suffix}@test.local',)).fetchone()['id']
        carer = db.execute("INSERT INTO staff(email,name,password_hash,role) VALUES "
                           "(%s,'照护员','x','carer') RETURNING id", (f'carer{suffix}@test.local',)).fetchone()['id']
        resident = db.execute("INSERT INTO residents(code,name,room,unit,care_plan) VALUES "
                              "(%s,'测试住户','999-A','东翼一层','测试用照护计划') RETURNING id",
                              (f'ZH-99{8 if suffix else 9}',)).fetchone()['id']
        alert = db.execute("INSERT INTO alerts(resident_id,status,priority) VALUES (%s,'confirmed',2) RETURNING id",
                           (resident,)).fetchone()['id']
    resp = client.post(f'/alerts/{alert}/family-contact',
                       json={'contact_role': 'primary', 'purpose': '核对夜间呼叫记录并确认家属安排'},
                       headers=auth(lead_a))
    assert resp.status_code == 201, resp.text
    contact = resp.json()['id']
    resp = client.post(f'/family-contacts/{contact}/attempts',
                       json={'expected_attempt_id': 0, 'channel': 'phone', 'outcome': 'no_answer',
                             'note': '两次拨打均未接通，约定稍后回拨'},
                       headers=auth(carer))
    assert resp.status_code == 201, resp.text
    return {'lead_a': lead_a, 'lead_b': lead_b, 'carer': carer, 'alert': alert, 'contact': contact}


def make_due(contact, minutes=-1):
    with connect() as db:
        db.execute("UPDATE family_contacts SET next_contact_at=now() + make_interval(mins => %s) WHERE id=%s",
                   (minutes, contact))


def latest_attempt(contact):
    with connect() as db:
        return db.execute('SELECT COALESCE(max(id),0) AS id FROM family_contact_attempts WHERE contact_id=%s',
                          (contact,)).fetchone()['id']


def claim(client, staff_id, contact, key=None, appointment=1):
    return client.post(f'/family-contacts/{contact}/claims',
                       json={'claim_key': key or uuid.uuid4().hex, 'appointment': appointment},
                       headers=auth(staff_id))


def queue(client, staff_id):
    resp = client.get('/callback-queue', headers=auth(staff_id))
    assert resp.status_code == 200, resp.text
    return resp.json()['items']


def test_queue_shows_appointment_claimer_and_due_time(client):
    ids = bootstrap(client)
    make_due(ids['contact'])
    items = queue(client, ids['lead_a'])
    assert len(items) == 1
    row = items[0]
    assert row['due'] and row['claim_id'] is None and row['claimer_name'] is None
    assert row['last_outcome'] == 'no_answer' and row['last_recorder'] == '照护员'
    assert row['next_contact_at'] is not None
    resp = claim(client, ids['lead_a'], ids['contact'])
    assert resp.status_code == 201, resp.text
    row = queue(client, ids['lead_a'])[0]
    assert row['claimer_name'] == '甲班值班长' and row['claim_expires_at'] is not None


def test_claim_requires_due_callback_and_current_appointment(client):
    ids = bootstrap(client)
    assert claim(client, ids['lead_a'], ids['contact']).status_code == 409  # 未到点
    make_due(ids['contact'])
    assert claim(client, ids['lead_a'], ids['contact'], appointment=0).status_code == 409  # 预约号过期
    assert claim(client, ids['carer'], ids['contact']).status_code == 403  # 仅值班长领取
    assert claim(client, ids['lead_a'], ids['contact']).status_code == 201


def test_claim_retry_does_not_double_occupy(client):
    ids = bootstrap(client)
    make_due(ids['contact'])
    key = uuid.uuid4().hex
    first = claim(client, ids['lead_a'], ids['contact'], key=key)
    assert first.status_code == 201, first.text
    retry = claim(client, ids['lead_a'], ids['contact'], key=key)
    assert retry.status_code == 201, retry.text
    assert retry.json()['id'] == first.json()['id']
    with connect() as db:
        active = db.execute("SELECT count(*) AS n FROM callback_claims WHERE contact_id=%s AND status='active'",
                            (ids['contact'],)).fetchone()['n']
    assert active == 1
    # 同一标识不能挪用到其他回拨事项
    other = bootstrap(client, suffix='-2')
    make_due(other['contact'])
    assert claim(client, other['lead_a'], other['contact'], key=key).status_code == 409


def test_concurrent_claims_only_one_succeeds(client):
    ids = bootstrap(client)
    make_due(ids['contact'])
    barrier = threading.Barrier(2)
    results = []

    def race(staff_id):
        barrier.wait(timeout=5)
        results.append(claim(client, staff_id, ids['contact']).status_code)

    threads = [threading.Thread(target=race, args=(ids['lead_a'],)),
               threading.Thread(target=race, args=(ids['lead_b'],))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)
    assert sorted(results) == [201, 409]
    row = queue(client, ids['lead_a'])[0]
    assert row['claim_id'] is not None


def test_direct_registration_supersedes_claim_and_late_receipt_fails(client):
    ids = bootstrap(client)
    make_due(ids['contact'])
    claim_id = claim(client, ids['lead_a'], ids['contact']).json()['id']
    # 班长（另一班值班长）直接登记改约，旧预约立即退出待领
    resp = client.post(f"/family-contacts/{ids['contact']}/attempts",
                       json={'expected_attempt_id': latest_attempt(ids['contact']), 'channel': 'phone',
                             'outcome': 'requested_callback', 'note': '家属请晚班再拨，改约到晚上'},
                       headers=auth(ids['lead_b']))
    assert resp.status_code == 201, resp.text
    with connect() as db:
        old = db.execute('SELECT status FROM callback_claims WHERE id=%s', (claim_id,)).fetchone()
        assert old['status'] == 'superseded'
        contact = db.execute('SELECT appointment, status FROM family_contacts WHERE id=%s',
                             (ids['contact'],)).fetchone()
        assert contact['appointment'] == 2 and contact['status'] == 'callback'
    # 旧领取人迟交的回执不能盖掉新结果
    resp = client.post(f'/callback-claims/{claim_id}/receipt',
                       json={'channel': 'phone', 'outcome': 'reached', 'note': '旧领取人迟到的回执'},
                       headers=auth(ids['lead_a']))
    assert resp.status_code == 409, resp.text
    with connect() as db:
        contact = db.execute('SELECT status, appointment FROM family_contacts WHERE id=%s',
                             (ids['contact'],)).fetchone()
        assert contact['status'] == 'callback' and contact['appointment'] == 2


def test_claim_timeout_restores_pool_and_blocks_stale_receipt(client):
    ids = bootstrap(client)
    make_due(ids['contact'])
    claim_id = claim(client, ids['lead_a'], ids['contact']).json()['id']
    with connect() as db:  # 领取后中断：租约超时
        db.execute("""UPDATE callback_claims SET claimed_at=now() - make_interval(mins => 30),
                             expires_at=now() - make_interval(mins => 1) WHERE id=%s""",
                   (claim_id,))
    row = queue(client, ids['lead_b'])[0]
    assert row['claim_id'] is None and row['due']  # 超时恢复待领
    resp = claim(client, ids['lead_b'], ids['contact'])
    assert resp.status_code == 201, resp.text
    resp = client.post(f'/callback-claims/{claim_id}/receipt',
                       json={'channel': 'phone', 'outcome': 'reached', 'note': '超时领取人的迟交回执'},
                       headers=auth(ids['lead_a']))
    assert resp.status_code == 409, resp.text
    with connect() as db:
        contact = db.execute('SELECT status FROM family_contacts WHERE id=%s', (ids['contact'],)).fetchone()
        assert contact['status'] == 'callback'  # 新结果未被盖掉


def test_receipt_completes_claim_and_clears_queue(client):
    ids = bootstrap(client)
    make_due(ids['contact'])
    claim_id = claim(client, ids['lead_a'], ids['contact']).json()['id']
    resp = client.post(f'/callback-claims/{claim_id}/receipt',
                       json={'channel': 'phone', 'outcome': 'reached', 'note': '已接通并核对夜间呼叫记录'},
                       headers=auth(ids['lead_a']))
    assert resp.status_code == 201, resp.text
    assert resp.json()['status'] == 'reached'
    assert queue(client, ids['lead_a']) == []
    with connect() as db:
        claim_row = db.execute("SELECT status, attempt_id FROM callback_claims WHERE id=%s", (claim_id,)).fetchone()
        assert claim_row['status'] == 'done' and claim_row['attempt_id'] is not None


def test_claim_blocks_other_carers_but_not_lead(client):
    ids = bootstrap(client)
    make_due(ids['contact'])
    claim(client, ids['lead_a'], ids['contact'])
    attempt = latest_attempt(ids['contact'])
    resp = client.post(f"/family-contacts/{ids['contact']}/attempts",
                       json={'expected_attempt_id': attempt, 'channel': 'in_person',
                             'outcome': 'reached', 'note': '家属到中心当面核对'},
                       headers=auth(ids['carer']))
    assert resp.status_code == 409  # 照护员需等领取超时
    resp = client.post(f"/family-contacts/{ids['contact']}/attempts",
                       json={'expected_attempt_id': attempt, 'channel': 'in_person',
                             'outcome': 'reached', 'note': '值班长当面确认接通'},
                       headers=auth(ids['lead_b']))
    assert resp.status_code == 201  # 班长仍可直接登记接通


def test_due_callbacks_survive_restart(client):
    ids = bootstrap(client)
    make_due(ids['contact'])
    claim_id = claim(client, ids['lead_a'], ids['contact']).json()['id']
    with TestClient(main.app) as restarted:  # 重启后重新迁移并加载同一数据库
        items = queue(restarted, ids['lead_b'])
        assert len(items) == 1 and items[0]['due'] and items[0]['claimer_name'] == '甲班值班长'
        resp = restarted.post(f'/callback-claims/{claim_id}/receipt',
                              json={'channel': 'phone', 'outcome': 'reached', 'note': '重启后照常提交回执'},
                              headers=auth(ids['lead_a']))
        assert resp.status_code == 201, resp.text


def test_release_returns_callback_to_pool(client):
    ids = bootstrap(client)
    make_due(ids['contact'])
    claim_id = claim(client, ids['lead_a'], ids['contact']).json()['id']
    resp = client.post(f'/callback-claims/{claim_id}/release', headers=auth(ids['lead_b']))
    assert resp.status_code == 403  # 仅领取人可释放
    resp = client.post(f'/callback-claims/{claim_id}/release', headers=auth(ids['lead_a']))
    assert resp.status_code == 200, resp.text
    row = queue(client, ids['lead_b'])[0]
    assert row['claim_id'] is None and row['due']
    assert claim(client, ids['lead_b'], ids['contact']).status_code == 201
