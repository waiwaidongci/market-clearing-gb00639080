"""Seal reviewed reports with a verifiable credential and rebuild originals on demand."""

import hashlib
import json
import logging
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.config.settings import ARTIFACTS
from src.domain.analysis import analyze
from src.domain.spec import SPEC
from src.models.jobs import ReportSeal, Run
from src.models.source import Dataset
from src.services.audit import record
from src.utils.exports import LAYOUT_VERSION, atomic_write, cleanup_stale, layout_for, render_bytes

log = logging.getLogger(__name__)

VOLATILE_KEYS = ('generated_at', 'duration_ms')


def _digest(data):
    return hashlib.sha256(data).hexdigest()


def rules_digest():
    """Fingerprint the clearing rules and layout that shape the report body."""
    rules = {
        'layout': LAYOUT_VERSION,
        'fields': SPEC['fields'],
        'params': SPEC['params'],
        'methodology': SPEC['methodology'],
    }
    return _digest(json.dumps(rules, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode())


def _snapshot(result):
    """Deep copy without volatile fields so a rebuild stays byte-identical."""
    clean = {key: value for key, value in result.items() if key not in VOLATILE_KEYS}
    return json.loads(json.dumps(clean, ensure_ascii=False))


def seal(db: Session, item: Run, actor):
    """Record an accepted report's credential and pin the on-disk bytes.

    The canonical bytes are rendered from the stored result under the run's own
    layout version, so the credential describes the true original rather than
    whatever happens to be on disk. A diverging file is restored immediately and
    the repair is audited.
    """
    existing = db.scalar(select(ReportSeal).where(ReportSeal.run_id == item.id))
    if existing:
        return existing, False
    if not item.result:
        raise ValueError('缺少计算结果，无法留下校验凭据')
    if not item.export_file:
        raise ValueError('报告文件缺失，无法留下校验凭据')
    version = layout_for(ARTIFACTS / item.export_file)
    snapshot = _snapshot(item.result)
    # Derive the checksum from the same snapshot a rebuild will replay, so the
    # credential and the restore path can never disagree by construction.
    canonical = render_bytes(version, snapshot)
    dataset = db.get(Dataset, item.dataset_id)
    seal_row = ReportSeal(
        run_id=item.id, filename=item.export_file, checksum=_digest(canonical), size=len(canonical),
        layout_version=version, rules_digest=rules_digest(),
        dataset_id=item.dataset_id, dataset_checksum=dataset.checksum if dataset else '',
        params=dict(item.params), result=snapshot, sealed_by=actor.username)
    db.add(seal_row)
    path = ARTIFACTS / item.export_file
    current = path.read_bytes() if path.is_file() else None
    if current != canonical:
        atomic_write(path, canonical)
        record(db, actor.username, 'report.repaired', item.id, checksum=seal_row.checksum)
    record(db, actor.username, 'report.sealed', item.id, checksum=seal_row.checksum,
           layout_version=version)
    return seal_row, True


@dataclass
class Rebuild:
    """Outcome of one rebuild attempt."""

    data: bytes | None
    reason: str | None = None
    source: str | None = None


@dataclass
class Status:
    state: str
    reason: str | None = None
    checksum: str | None = None
    expected: str | None = None


def inspect(seal_row: ReportSeal) -> Status:
    """Classify the on-disk report against its credential."""
    path = ARTIFACTS / seal_row.filename
    if not path.is_file():
        return Status('missing', '报告文件已删除，可按凭据重建', expected=seal_row.checksum)
    actual = _digest(path.read_bytes())
    if actual != seal_row.checksum:
        return Status('altered', '报告文件已被改动，可按凭据重建', checksum=actual, expected=seal_row.checksum)
    return Status('intact', checksum=actual, expected=seal_row.checksum)


def rebuild(db: Session, seal_row: ReportSeal) -> Rebuild:
    """Re-render the original bytes from the sealed batch, rules and layout.

    The sealed result snapshot plus the pinned layout version reproduce the
    original even after the clearing rules change, so a restored report keeps
    its number and its review conclusion. Only when no snapshot exists do we
    recompute with the live rules, and the checksum still has the last word.
    """
    if seal_row.result is not None:
        data = render_bytes(seal_row.layout_version, seal_row.result)
        if _digest(data) == seal_row.checksum:
            return Rebuild(data, source='snapshot')
        return Rebuild(None, '按凭据快照重建的内容与校验不一致', source='snapshot')
    dataset = db.get(Dataset, seal_row.dataset_id)
    if not dataset:
        return Rebuild(None, '凭据引用的数据批次已不存在', source='recomputed')
    if dataset.checksum != seal_row.dataset_checksum:
        return Rebuild(None, '数据批次内容与凭据不一致', source='recomputed')
    try:
        fresh = analyze(dataset.rows, seal_row.params)
    except ValueError as error:
        return Rebuild(None, f'按现行规则重算失败：{error}', source='recomputed')
    data = render_bytes(seal_row.layout_version, fresh)
    if _digest(data) != seal_row.checksum:
        return Rebuild(None, '规则已更新，重建结果与凭据不一致，原件无法还原', source='recomputed')
    return Rebuild(data, source='recomputed')


def prepare_download(db: Session, seal_row: ReportSeal, actor: str) -> tuple[bytes | None, Status]:
    """Return bytes that match the credential, rebuilding in place if needed.

    The bytes actually served are the bytes verified, so a corrupted or swapped
    file can never slip out. The caller holds the run-row lock, so concurrent
    downloads serialize; staging is fsynced and atomically renamed, so every
    reader gets a complete file and an interrupted rebuild leaves the original
    untouched and can be retried.
    """
    cleanup_stale()
    path = ARTIFACTS / seal_row.filename
    try:
        payload = path.read_bytes()
    except FileNotFoundError:
        payload = None
    actual = _digest(payload) if payload is not None else None
    if actual is not None and actual == seal_row.checksum:
        return payload, Status('intact', checksum=actual, expected=seal_row.checksum)
    state = 'missing' if payload is None else 'altered'
    reason = '报告文件已删除，可按凭据重建' if payload is None else '报告文件已被改动，可按凭据重建'
    rebuilt = rebuild(db, seal_row)
    if rebuilt.data is None:
        record(db, actor, 'report.download.blocked', seal_row.run_id,
               state=state, reason=rebuilt.reason, expected=seal_row.checksum)
        return None, Status(state, rebuilt.reason, actual, seal_row.checksum)
    atomic_write(path, rebuilt.data)
    record(db, actor, 'report.restored', seal_row.run_id, state=state,
           checksum=seal_row.checksum, size=len(rebuilt.data), source=rebuilt.source)
    return rebuilt.data, Status('restored', f'已按凭据重建原件（{rebuilt.source}）',
                                checksum=seal_row.checksum, expected=seal_row.checksum)
