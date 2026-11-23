import csv
import io
import os
import time
from pathlib import Path

from src.config.settings import ARTIFACTS

# Bump when the CSV layout changes. Keep every released renderer below: a seal
# records the layout version it was written under so an original can always be
# reproduced byte for byte, even after the layout moves on.
LAYOUT_VERSION = '1'


def safe_cell(value):
    if isinstance(value, str) and (value.lstrip().startswith(('=', '+', '-', '@'))
                                   or value.startswith(('\t', '\r', '\n'))):
        return "'" + value
    return value


def _layout_v1(result):
    rows = [[safe_cell(result['headline'])], [safe_cell(result.get('note', ''))]]
    for metric in result['metrics']:
        rows.append([safe_cell(metric['label']), safe_cell(metric['value']), safe_cell(metric.get('detail', ''))])
    for table in result['tables']:
        rows.append([])
        rows.append([safe_cell(table['title'])])
        columns = table['columns']
        rows.append([safe_cell(column['label']) for column in columns])
        for row in table['rows']:
            rows.append([safe_cell(row.get(column['key'])) for column in columns])
    return rows


LAYOUTS = {LAYOUT_VERSION: _layout_v1}


def render_bytes(version, result):
    """Render a report with a pinned layout version and return the exact bytes."""
    try:
        layout = LAYOUTS[version]
    except KeyError:
        raise ValueError(f'报告版式版本 {version} 不可用，无法还原原件') from None
    buffer = io.StringIO(newline='')
    csv.writer(buffer).writerows(layout(result))
    return buffer.getvalue().encode('utf-8-sig')


def _staging(path):
    """A per-caller staging name so two rebuilds never share an inode."""
    return path.with_name(f'.{path.name}.{os.getpid()}.{time.time_ns()}.tmp')


def atomic_write(path, data):
    """Publish a complete file atomically: write+fsync staging, then rename.

    A reader either sees the previous complete file or the new complete file,
    never a partial one; an interrupted rebuild simply leaves the old bytes.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    staging = _staging(path)
    try:
        with staging.open('wb') as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.replace(staging, path)
    except BaseException:
        staging.unlink(missing_ok=True)
        raise
    try:
        handle = os.open(path.parent, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(handle)
    finally:
        os.close(handle)


def cleanup_stale(older_than=300):
    """Drop staging files left by an interrupted rebuild."""
    if not ARTIFACTS.is_dir():
        return
    cutoff = time.time() - older_than
    for item in ARTIFACTS.glob('.*.tmp'):
        try:
            if item.stat().st_mtime < cutoff:
                item.unlink()
        except OSError:
            continue


def write_report(run_id, attempt, result, version=LAYOUT_VERSION):
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    name = f'analysis-{run_id}-{attempt}.csv'
    atomic_write(ARTIFACTS / name, render_bytes(version, result))
    # Sidecar pins the layout a file was written under, so a later layout bump
    # can still replay the original bytes for an older run.
    atomic_write(path_for_layout(ARTIFACTS / name), version.encode())
    return name


def path_for_layout(path):
    return path.with_name(f'{path.stem}.layout')


def layout_for(path, fallback=LAYOUT_VERSION):
    sidecar = path_for_layout(path)
    try:
        version = sidecar.read_text().strip()
    except OSError:
        return fallback
    return version if version in LAYOUTS else fallback
