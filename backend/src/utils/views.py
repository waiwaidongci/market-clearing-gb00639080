from src.utils.time import iso


def dataset(item):
    return {'id': item.id, 'name': item.name, 'filename': item.filename, 'row_count': item.row_count,
            'checksum': item.checksum, 'author': item.author, 'created_at': iso(item.created_at)}


def run(item, detail=False, seal=None, seal_status=None):
    item_view = {'id': item.id, 'dataset_id': item.dataset_id, 'name': item.name, 'status': item.status,
                 'author': item.author, 'review': item.review, 'decision': item.decision,
                 'created_at': iso(item.created_at), 'finished_at': iso(item.finished_at),
                 'scheduled_at': iso(item.scheduled_at), 'schedule_id': item.schedule_id,
                 'has_export': bool(item.export_file), 'error': item.error,
                 'sealed': bool(seal), 'report_state': seal_status.state if seal_status else None}
    if detail:
        item_view.update(params=item.params, result=item.result, review_note=item.review_note)
        item_view['report'] = report(seal, seal_status)
    return item_view


def report(seal, status):
    if not seal:
        return {'sealed': False, 'state': 'unsealed',
                'detail': '此报告没有校验凭据，无法确认是否为复核通过的原件。'}
    return {'sealed': True, 'state': status.state if status else 'unknown',
            'detail': status.reason if status and status.reason else '校验凭据与报告文件一致。',
            'checksum': seal.checksum, 'expected': status.expected if status else seal.checksum,
            'size': seal.size, 'layout_version': seal.layout_version, 'rules_digest': seal.rules_digest,
            'sealed_by': seal.sealed_by, 'sealed_at': iso(seal.sealed_at)}


def schedule(item):
    from src.domain.spec import SPEC
    return {'id': item.id, 'name': item.name, 'dataset_id': item.dataset_id, 'recipient': item.recipient,
            'next_at': iso(item.next_at), 'repeat_daily': item.repeat_daily, 'enabled': item.enabled,
            'next_report_day': item.params.get(SPEC['schedule_date_param']) if item.enabled else None}


def delivery(item):
    return {'id': item.id, 'run_id': item.run_id, 'recipient': item.recipient,
            'state': item.state, 'detail': item.detail, 'created_at': iso(item.created_at)}
