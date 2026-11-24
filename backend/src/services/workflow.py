import hashlib
import uuid

from fastapi import HTTPException
from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import IntegrityError

from src.config.settings import cache, engine
from src.constants.workflow import RequestStatus, Status
from src.models.tables import audit, cases, dispatches, evidence, requests, runs
from src.services import domain
from src.utils.encoding import canonical
from src.utils.time import utc_time


def log(conn, actor, action, reference):
    conn.execute(audit.insert().values(actor=actor, action=action, reference=str(reference), created_at=utc_time()))


def recent(table):
    with engine.connect() as conn:
        return [dict(row) for row in conn.execute(select(table).order_by(table.c.id.desc()).limit(100)).mappings()]


def found(conn, table, identity):
    row = conn.execute(select(table).where(table.c.id == identity)).mappings().first()
    if row is None:
        raise HTTPException(404, '找不到该记录')
    return dict(row)


def make_case(title, payload, actor):
    title = title.strip()
    if not title:
        raise HTTPException(422, '档案名称不能为空')
    try:
        if len(canonical(payload)) > 512 * 1024:
            raise ValueError('业务输入超过 512 KiB')
        if callable(getattr(domain, 'validate', None)):
            domain.validate(payload)
    except (ValueError, TypeError) as exc:
        raise HTTPException(422, str(exc)) from None
    with engine.begin() as conn:
        case_id = conn.execute(cases.insert().values(slug=uuid.uuid4().hex, title=title, payload=payload,
                               author=actor, created_at=utc_time())).inserted_primary_key[0]
        log(conn, actor, 'create_case', case_id)
        return found(conn, cases, case_id)


def wake():
    try:
        cache.lpush('worker:wake', 'queued')
        cache.ltrim('worker:wake', 0, 99)
    except Exception:
        # The durable database queue is polled even if Redis is temporarily unavailable.
        pass


def request_key(payload):
    return hashlib.sha256(canonical(payload)).hexdigest()


def request_view(conn, application):
    run = found(conn, runs, application['run_id'])
    view = {'id': application['id'], 'slug': application['slug'], 'run_id': run['id'], 'case_id': run['case_id'],
            'owner': application['owner'], 'request_status': application['status'],
            'status': run['status'], 'created_at': application['created_at'],
            'withdrawn_at': application['withdrawn_at'], 'error': run['error'],
            'evidence_id': None, 'report': None}
    if application['status'] == RequestStatus.ACTIVE and run['status'] == Status.COMPLETE:
        row = conn.execute(select(evidence).where(evidence.c.run_id == run['id'])).mappings().first()
        if row:
            view['evidence_id'] = row['id']
            view['report'] = row['report']
    return view


def submit(case_id, actor, request_id=None):
    request_id = (request_id or '').strip() or None
    if request_id and len(request_id) > 48:
        raise HTTPException(422, '申请号须为 1 至 48 个字符')
    dispatched = False
    for attempt in range(4):
        dispatched = False
        try:
            with engine.begin() as conn:
                case = found(conn, cases, case_id)
                key = request_key(case['payload'])
                if request_id:
                    existing = conn.execute(select(requests).where(requests.c.slug == request_id)).mappings().first()
                    if existing:
                        if existing['request_key'] != key:
                            raise HTTPException(409, '该申请号已用于其他查验输入')
                        return request_view(conn, existing)
                dispatch = conn.execute(select(dispatches).where(dispatches.c.request_key == key)).mappings().first()
                run = None
                if dispatch:
                    run = conn.execute(select(runs).where(runs.c.id == dispatch['run_id'])
                                       .with_for_update()).mappings().first()
                    if run and run['status'] == Status.VOIDED:
                        run = None
                if run is None:
                    run_id = conn.execute(runs.insert().values(case_id=case_id, status=Status.QUEUED,
                                          owner=actor, created_at=utc_time())).inserted_primary_key[0]
                    if dispatch:
                        conn.execute(dispatches.delete().where(dispatches.c.request_key == key))
                    conn.execute(dispatches.insert().values(request_key=key, run_id=run_id, created_at=utc_time()))
                    log(conn, actor, 'enqueue', run_id)
                    dispatched = True
                else:
                    run_id = run['id']
                slug = request_id or uuid.uuid4().hex
                application_id = conn.execute(requests.insert().values(slug=slug, run_id=run_id, request_key=key,
                                              owner=actor, status=RequestStatus.ACTIVE,
                                              created_at=utc_time())).inserted_primary_key[0]
                log(conn, actor, 'submit_request', slug)
                view = request_view(conn, found(conn, requests, application_id))
        except IntegrityError:
            # A concurrent identical application won the dispatch race; retry attaches to it.
            if attempt == 3:
                raise HTTPException(409, '相同输入正在并发提交，请按原申请号重试') from None
            continue
        break
    if dispatched:
        wake()
    return view


def show_request(slug):
    with engine.connect() as conn:
        application = conn.execute(select(requests).where(requests.c.slug == slug)).mappings().first()
        if application is None:
            raise HTTPException(404, '找不到该申请')
        return request_view(conn, application)


def withdraw(slug, actor):
    with engine.begin() as conn:
        application = conn.execute(select(requests).where(requests.c.slug == slug)).mappings().first()
        if application is None:
            raise HTTPException(404, '找不到该申请')
        changed = conn.execute(requests.update().where(requests.c.id == application['id'],
                               requests.c.status == RequestStatus.ACTIVE)
                               .values(status=RequestStatus.WITHDRAWN, withdrawn_at=utc_time())).rowcount
        if changed:
            log(conn, actor, 'withdraw_request', slug)
            run = conn.execute(select(runs).where(runs.c.id == application['run_id'])
                               .with_for_update()).mappings().first()
            if run['status'] in (Status.QUEUED, Status.RUNNING):
                remaining = conn.execute(select(func.count()).select_from(requests).where(
                    requests.c.run_id == run['id'], requests.c.status == RequestStatus.ACTIVE)).scalar_one()
                if remaining == 0:
                    conn.execute(runs.update().where(runs.c.id == run['id'])
                                 .values(status=Status.VOIDED, finished_at=utc_time()))
                    conn.execute(dispatches.delete().where(dispatches.c.request_key == application['request_key']))
                    log(conn, actor, 'void_run', run['id'])
        return request_view(conn, found(conn, requests, application['id']))


def claim():
    ready = or_(runs.c.status == Status.QUEUED,
                and_(runs.c.status == Status.RUNNING, runs.c.started_at < utc_time(-600)))
    with engine.begin() as conn:
        run = conn.execute(select(runs.c.id, runs.c.case_id).where(ready).order_by(runs.c.id)
                           .limit(1).with_for_update(skip_locked=True)).first()
        if not run:
            return None
        token = str(uuid.uuid4())
        conn.execute(runs.update().where(runs.c.id == run.id).values(
            started_at=utc_time(), status=Status.RUNNING, attempt_token=token))
        return run.id, token, found(conn, cases, run.case_id)['payload']


def finish(run_id, token, result=None, error=None):
    digest = hashlib.sha256(canonical(result)).hexdigest() if result is not None else None
    status = Status.FAILED if error else Status.COMPLETE
    with engine.begin() as conn:
        count = conn.execute(runs.update().where(runs.c.id == run_id,
                        runs.c.attempt_token == token, runs.c.status == Status.RUNNING)
                        .values(status=status, finished_at=utc_time(), error=error)).rowcount
        if not count:
            return
        if result is not None:
            conn.execute(evidence.insert().values(run_id=run_id, report=result, digest=digest))
        log(conn, 'worker', status.value, run_id)


def review(identity, note, actor):
    note = note.strip()
    if not note:
        raise HTTPException(422, '复核意见不能为空')
    with engine.begin() as conn:
        found(conn, evidence, identity)
        count = conn.execute(evidence.update().where(evidence.c.id == identity, evidence.c.reviewed_at.is_(None))
                            .values(review_note=note, reviewer=actor, reviewed_at=utc_time())).rowcount
        if count != 1:
            raise HTTPException(409, '该证据已经复核')
        log(conn, actor, 'review', identity)
        return found(conn, evidence, identity)
