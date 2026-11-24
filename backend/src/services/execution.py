import logging
import time
from datetime import date, timedelta
from sqlalchemy import and_, or_, select
from src.config.database import Session
from src.constants.enums import Status
from src.domain import analysis, outage
from src.domain.spec import SPEC
from src.models.jobs import Delivery, Run, Schedule
from src.models.source import Dataset
from src.services.audit import record
from src.services.outage import DatabaseCache, load_snapshot, save_snapshot
from src.utils.exports import write_report
from src.utils.time import now

log = logging.getLogger(__name__)
LEASE_SECONDS = 180
WAIT_SECONDS = 150


def materialize_due():
    if not SPEC.get('scheduled'):
        return
    stamp = now()
    with Session.begin() as db:
        schedules = db.scalars(select(Schedule).where(Schedule.enabled.is_(True), Schedule.next_at <= stamp)
                                .order_by(Schedule.next_at).limit(20).with_for_update(skip_locked=True))
        for item in schedules:
            due = item.next_at
            dataset_id = item.dataset_id
            params = dict(item.params)
            if item.repeat_daily:
                elapsed_days = (stamp - due).days
                due += timedelta(days=elapsed_days)
                period_key = SPEC['schedule_date_param']
                report_day = date.fromisoformat(params[period_key]) + timedelta(days=elapsed_days)
                params[period_key] = report_day.isoformat()
                item.params = {**params, period_key: (report_day + timedelta(days=1)).isoformat()}
                dataset_id = db.scalar(select(Dataset.id).order_by(Dataset.id.desc()).limit(1)) or dataset_id
                item.next_at = due + timedelta(days=1)
            else:
                item.enabled = False
            db.add(Run(dataset_id=dataset_id, schedule_id=item.id, scheduled_at=due,
                       name=item.name, params=params, author=item.author))


def claim():
    stamp = now()
    with Session.begin() as db:
        query = select(Run).where(or_(Run.status == Status.QUEUED,
                                      and_(Run.status == Status.RUNNING, Run.lease_until < stamp)))
        job = db.scalar(query.order_by(Run.id).limit(1).with_for_update(skip_locked=True))
        if job is None:
            return None
        if job.attempt >= 3:
            job.status = Status.FAILED
            job.error = '后台进程多次中断，请发起新的运行'
            job.finished_at = stamp
            record(db, 'worker', 'analysis.failed', job.id, reason=job.error)
            return None
        job.status = Status.RUNNING
        job.attempt += 1
        job.lease_until = stamp + timedelta(seconds=LEASE_SECONDS)
        records = db.get(Dataset, job.dataset_id).rows
        _, snapshot = load_snapshot(db, job.id)
        return job.id, job.attempt, records, job.params, snapshot


def _ensure_snapshot(job_id, attempt, rows):
    """老运行没有开算快照时，按当前批次补建一次；已有快照则原样返回。"""
    with Session.begin() as db:
        job = db.get(Run, job_id, with_for_update=True)
        if job.attempt != attempt or job.status != Status.RUNNING:
            return None
        _, snapshot = load_snapshot(db, job_id)
        if snapshot is None:
            snapshot = analysis.snapshot_for(rows)
            save_snapshot(db, job_id, snapshot)
        return snapshot


def _renewer(job_id, attempt):
    """按需延长租约，长时间枚举偏移组合时不被其他进程抢走。"""
    last = [0.0]

    def renew(done, total):
        stamp = time.monotonic()
        if stamp - last[0] < 20:
            return
        last[0] = stamp
        with Session.begin() as db:
            job = db.get(Run, job_id, with_for_update=True)
            if job.attempt == attempt and job.status == Status.RUNNING:
                job.lease_until = now() + timedelta(seconds=LEASE_SECONDS)

    return renew


def execute(lease):
    job_id, attempt, rows, params, snapshot = lease
    result = export = failure = None
    resumed = snapshot is not None
    try:
        # 中断后恢复只用开算时保存的两路记录、范围和灯数，不再依赖批次重新聚合。
        if snapshot is None:
            snapshot = _ensure_snapshot(job_id, attempt, rows)
        cache = DatabaseCache(snapshot) if outage.has_outage(snapshot) else None
        result = analysis.analyze(rows, params, outage_cache=cache, snapshot=snapshot,
                                  progress=_renewer(job_id, attempt), wait=WAIT_SECONDS)
        export = write_report(job_id, attempt, result)
    except outage.Incomplete as error:
        log.info('Analysis %s paused: %s', job_id, error)
        # 并发接手时等待其他进程验完组合；本轮不计入中断次数，稍后重新领取。
        with Session.begin() as db:
            job = db.get(Run, job_id, with_for_update=True)
            if job.attempt == attempt and job.status == Status.RUNNING:
                job.status = Status.QUEUED
                job.lease_until = None
                job.attempt = max(0, attempt - 1)
                record(db, 'worker', 'analysis.paused', job.id, attempt=attempt, reason=str(error)[:500])
        return
    except ValueError as error:
        failure = str(error)[:500]
    except Exception:
        log.exception('Analysis %s failed', job_id)
        failure = '计算失败，请检查字段口径后重试'
    with Session.begin() as db:
        job = db.get(Run, job_id, with_for_update=True)
        if job.attempt != attempt or job.status != Status.RUNNING:
            return
        job.lease_until = None
        job.finished_at = now()
        job.status = Status.FAILED if failure else Status.COMPLETE
        job.result = None if failure else result
        job.export_file = export
        job.error = failure
        record(db, 'worker', f'analysis.{job.status}', job.id, attempt=attempt, reason=failure,
               resumed=resumed)
        if job.schedule_id:
            plan = db.get(Schedule, job.schedule_id)
            ready = not failure and result.get('ready', True)
            detail = '已保存 CSV 到站内收件箱' if ready else (failure or result['headline'])
            receipt = Delivery(run_id=job.id, recipient=plan.recipient, state='delivered' if ready else 'blocked', detail=detail[:500])
            db.add(receipt)
            record(db, 'worker', f'delivery.{receipt.state}', job.id, recipient=plan.recipient)
