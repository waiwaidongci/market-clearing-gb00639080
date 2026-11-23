"""Route-level checks for review sealing and the verified download path."""

import hashlib

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from src.config.database import Session
from src.config.settings import ARTIFACTS
from src.domain.analysis import analyze
from src.domain.spec import SPEC
from src.models.jobs import ReportSeal, Run
from src.models.source import Dataset
from src.utils.exports import write_report
from src.utils.security import token

DAY = SPEC['fixture']['posting_date']


def _digest(data):
    return hashlib.sha256(data).hexdigest()


@pytest.fixture(scope='module')
def client():
    from src.main import app
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture(scope='module')
def admin_headers(client):
    return {'Authorization': f'Bearer {token("admin", "admin")}'}


def _complete_run():
    with Session.begin() as db:
        dataset = db.scalar(select(Dataset).order_by(Dataset.id))
        result = analyze(dataset.rows, {'posting_date': DAY})
        run = Run(dataset_id=dataset.id, name='api-run', params={'posting_date': DAY},
                  author='analyst', status='complete', result=result)
        db.add(run)
        db.flush()
        run.export_file = write_report(run.id, 1, result)
        return run.id


def _accept(client, headers, run_id):
    return client.post(f'/runs/{run_id}/review', headers=headers, json={
        'review': 'accepted', 'note': '勾稽表已核对', 'decision': '确认入账'})


def test_review_accepts_and_leaves_credential(client, admin_headers):
    run_id = _complete_run()
    response = _accept(client, admin_headers, run_id)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body['sealed'] is True
    assert body['report']['state'] == 'intact'
    assert body['report']['checksum']
    with Session.begin() as db:
        seal_row = db.scalar(select(ReportSeal).where(ReportSeal.run_id == run_id))
        assert seal_row is not None
        assert seal_row.checksum == body['report']['checksum']


def test_download_after_delete_rebuilds_same_number(client, admin_headers):
    run_id = _complete_run()
    assert _accept(client, admin_headers, run_id).status_code == 200
    with Session.begin() as db:
        seal_row = db.scalar(select(ReportSeal).where(ReportSeal.run_id == run_id))
        filename = seal_row.filename
        checksum = seal_row.checksum
    path = ARTIFACTS / filename
    original = path.read_bytes()
    path.unlink()

    response = client.get(f'/runs/{run_id}/export', headers=admin_headers)
    assert response.status_code == 200, response.text
    assert response.content == original
    assert response.headers['X-Report-Checksum'] == checksum
    assert response.headers['X-Report-Integrity'] == 'verified'
    assert path.read_bytes() == original
    assert _digest(response.content) == checksum


def test_download_after_tamper_serves_original_not_tampered(client, admin_headers):
    run_id = _complete_run()
    assert _accept(client, admin_headers, run_id).status_code == 200
    with Session.begin() as db:
        seal_row = db.scalar(select(ReportSeal).where(ReportSeal.run_id == run_id))
        filename, checksum = seal_row.filename, seal_row.checksum
    path = ARTIFACTS / filename
    original = path.read_bytes()
    path.write_bytes(original + '篡改'.encode())

    response = client.get(f'/runs/{run_id}/export', headers=admin_headers)
    assert response.status_code == 200
    assert response.content == original
    assert _digest(response.content) == checksum


def test_download_blocks_and_explains_when_unverifiable(client, admin_headers):
    run_id = _complete_run()
    assert _accept(client, admin_headers, run_id).status_code == 200
    with Session.begin() as db:
        seal_row = db.scalar(select(ReportSeal).where(ReportSeal.run_id == run_id))
        seal_row.checksum = '0' * 64
        filename = seal_row.filename
    (ARTIFACTS / filename).unlink()

    response = client.get(f'/runs/{run_id}/export', headers=admin_headers)
    assert response.status_code == 409
    assert '暂停下载' in response.json()['detail']


def test_legacy_unsealed_run_is_explicitly_flagged(client, admin_headers):
    run_id = _complete_run()
    body = client.get(f'/runs/{run_id}', headers=admin_headers).json()
    assert body['sealed'] is False
    assert body['report']['state'] == 'unsealed'
    assert '没有校验凭据' in body['report']['detail']
    # The legacy file remains downloadable, marked as unsealed.
    response = client.get(f'/runs/{run_id}/export', headers=admin_headers)
    assert response.status_code == 200
    assert response.headers['X-Report-Integrity'] == 'unsealed'
