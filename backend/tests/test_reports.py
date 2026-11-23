"""End-to-end checks for report credentials: seal, tamper, rebuild, concurrency.

Runs against SQLite plus a temporary artifact directory; it exercises the real
service code, not mocks.
"""

import hashlib
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.config.database import Base
from src.config.settings import ARTIFACTS
from src.domain.analysis import analyze
from src.domain.demo import demo_rows
from src.domain.spec import SPEC
from src.models.jobs import ReportSeal, Run
from src.models.source import Dataset
from src.services import reports
from src.utils.exports import LAYOUT_VERSION, atomic_write, render_bytes, write_report

DAY = SPEC['fixture']['posting_date']


@pytest.fixture()
def db():
    from sqlalchemy.pool import StaticPool
    engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Factory = sessionmaker(engine, expire_on_commit=False)
    with Factory.begin() as session:
        yield session


def _digest(data):
    return hashlib.sha256(data).hexdigest()


def _ready(db, day=DAY):
    rows = demo_rows()
    dataset = Dataset(name='demo', filename='demo.csv', checksum='abc', rows=rows,
                      row_count=len(rows), author='seed')
    db.add(dataset)
    db.flush()
    result = analyze(rows, {'posting_date': day})
    run = Run(dataset_id=dataset.id, name='run', params={'posting_date': day}, author='analyst',
              status='complete', result=result)
    db.add(run)
    db.flush()
    run.export_file = write_report(run.id, 1, result)
    db.flush()
    return run


def _seal(db, run):
    seal_row, _ = reports.seal(db, run, type('A', (), {'username': 'admin'})())
    db.flush()
    return seal_row


def test_seal_matches_on_disk_report(db):
    run = _ready(db)
    seal_row = _seal(db, run)
    path = ARTIFACTS / seal_row.filename
    assert _digest(path.read_bytes()) == seal_row.checksum
    assert reports.inspect(seal_row).state == 'intact'


def test_deleted_report_rebuilds_original_bytes(db):
    run = _ready(db)
    seal_row = _seal(db, run)
    path = ARTIFACTS / seal_row.filename
    original = path.read_bytes()
    path.unlink()

    data, status = reports.prepare_download(db, seal_row, 'viewer')
    assert status.state == 'restored'
    assert data == original
    assert path.read_bytes() == original
    assert reports.inspect(seal_row).state == 'intact'


def test_altered_report_restores_original_and_blocks_tampered_bytes(db):
    run = _ready(db)
    seal_row = _seal(db, run)
    path = ARTIFACTS / seal_row.filename
    original = path.read_bytes()
    path.write_bytes(original.replace('结算检查'.encode(), '结算合格'.encode()))

    data, status = reports.prepare_download(db, seal_row, 'viewer')
    assert status.state == 'restored'
    assert data == original
    assert reports.inspect(seal_row).state == 'intact'


def test_rule_update_still_rebuilds_original(db, monkeypatch):
    run = _ready(db)
    seal_row = _seal(db, run)
    path = ARTIFACTS / seal_row.filename
    original = path.read_bytes()

    # Simulate a rules change: the live digest no longer matches the seal.
    monkeypatch.setattr(reports, 'rules_digest', lambda: 'changed')
    assert reports.rules_digest() != seal_row.rules_digest
    path.unlink()

    data, status = reports.prepare_download(db, seal_row, 'viewer')
    assert status.state == 'restored'
    assert data == original
    assert path.read_bytes() == original


def test_unreconstructable_report_blocks_with_reason(db):
    run = _ready(db)
    seal_row = _seal(db, run)
    (ARTIFACTS / seal_row.filename).unlink()
    # Corrupt the credential so no rebuild can match it.
    seal_row.checksum = '0' * 64
    db.flush()

    data, status = reports.prepare_download(db, seal_row, 'viewer')
    assert data is None
    assert status.state == 'missing'
    assert status.reason


def test_interrupted_rebuild_leaves_original_and_recovers(db):
    run = _ready(db)
    seal_row = _seal(db, run)
    path = ARTIFACTS / seal_row.filename
    original = path.read_bytes()
    path.unlink()

    # A crash mid-rebuild leaves only a staging file: the target stays absent.
    staging = path.with_name(f'.{path.name}.999.1.tmp')
    staging.write_bytes(b'partial')
    assert not path.exists()

    data, status = reports.prepare_download(db, seal_row, 'viewer')
    assert status.state == 'restored'
    assert data == original
    # A recent staging file may belong to a live rebuild for another run and is
    # kept; once stale, the next download sweeps it away.
    assert staging.exists()
    old = time.time() - 3600
    staging.touch()
    import os
    os.utime(staging, (old, old))
    import src.utils.exports as exports
    exports.cleanup_stale()
    assert not staging.exists()


def test_concurrent_downloads_all_get_verified_original(db):
    run = _ready(db)
    seal_row = _seal(db, run)
    path = ARTIFACTS / seal_row.filename
    original = path.read_bytes()
    path.unlink()

    def work(_):
        data, status = reports.prepare_download(db, seal_row, 'viewer')
        return data

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(work, range(16)))
    assert all(item == original for item in results)
    assert _digest(path.read_bytes()) == seal_row.checksum


def test_legacy_unsealed_report_is_flagged(db):
    run = _ready(db)
    assert reports.inspect is not None
    from src.utils import views
    view = views.run(run, detail=True)
    assert view['sealed'] is False
    assert view['report']['state'] == 'unsealed'
    assert '凭据' in view['report']['detail']


def test_seal_is_idempotent(db):
    run = _ready(db)
    first = _seal(db, run)
    second, created = reports.seal(db, run, type('A', (), {'username': 'other'})())
    assert created is False
    assert second.id == first.id
