"""夜间停灯的开算快照与已验偏移组合持久化。

快照只保存开算时的两路记录、校时范围和灯数，中断恢复不依赖原始批次是否变化。
组合缓存以快照指纹分组，已验组合落库后其他进程直接复用，不必重算。
"""

import hashlib
import json

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert
from src.config.database import Session
from src.models.jobs import OutageCheck, OutageSnapshot


def snapshot_key(snapshot):
    raw = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
    return hashlib.sha256(raw.encode()).hexdigest()


def lock_key(key):
    return int(key[:16], 16) % (2 ** 63)


def save_snapshot(db, run_id, snapshot):
    """把开算时的两路记录、范围和灯数系到运行记录上（只写一次）。"""
    key = snapshot_key(snapshot)
    existing = db.get(OutageSnapshot, run_id)
    if existing is None:
        db.add(OutageSnapshot(run_id=run_id, key=key, snapshot=snapshot))
        db.flush()
    return key


def load_snapshot(db, run_id):
    item = db.get(OutageSnapshot, run_id)
    return (item.key, item.snapshot) if item else (None, None)


class DatabaseCache:
    """把偏移组合校验结果存入 outage_checks，并用协作锁序列化补验。"""

    def __init__(self, snapshot, key=None):
        self.key = key or snapshot_key(snapshot)
        self.lock = lock_key(self.key)
        self._session = None

    def acquire(self):
        db = Session()
        got = db.execute(text('SELECT pg_try_advisory_lock(:key)'), {'key': self.lock}).scalar()
        if not got:
            db.close()
            return False
        self._session = db
        return True

    def release(self):
        if self._session is not None:
            try:
                self._session.execute(text('SELECT pg_advisory_unlock(:key)'), {'key': self.lock})
                self._session.commit()
            finally:
                self._session.close()
                self._session = None

    def load(self):
        with Session() as db:
            rows = db.scalars(select(OutageCheck).where(OutageCheck.snapshot_key == self.key))
            return {(row.night, row.hotline, row.inspection): row.payload for row in rows}

    def put_many(self, payloads):
        if not payloads:
            return
        records = [
            {'snapshot_key': self.key, 'night': key[0], 'hotline': key[1], 'inspection': key[2],
             'payload': payload}
            for key, payload in payloads
        ]
        with Session.begin() as db:
            statement = insert(OutageCheck).values(records)
            db.execute(statement.on_conflict_do_nothing(
                index_elements=['snapshot_key', 'night', 'hotline', 'inspection']))

