import {REPORT} from '../../constants/enums.js';
import {el, p} from '../../utils/dom.js';
import {date} from '../../utils/format.js';
import {pill} from './panels.js';

// The download endpoint rebuilds and re-verifies before streaming, so a missing
// or altered file is restored to a credential-matching original rather than
// blocked here. Only an unreconstructable report returns 409 and pauses download.
export function reportProof(report) {
  if (!report) return null;
  if (!report.sealed) {
    return el('div', {class:'report-proof', 'data-testid':'report-proof'},
      pill(REPORT.unsealed, 'unsealed'), p(report.detail, 'subtle'),
      p('此运行早于凭据启用，下载内容仍可用，但无法据凭据确认未被改动。', 'subtle'));
  }
  return el('div', {class:'report-proof', 'data-testid':'report-proof'},
    pill(REPORT[report.state] || REPORT.unknown, report.state), p(report.detail, 'subtle'),
    el('dl', {},
      el('dt', {}, 'SHA-256（应一致）'), el('dd', {}, el('code', {}, report.expected || '—')),
      el('dt', {}, '当前文件摘要'), el('dd', {}, el('code', {}, report.checksum || '—')),
      el('dt', {}, '版式'), el('dd', {}, report.layout_version || '—'),
      el('dt', {}, '签核人'), el('dd', {}, `${report.sealed_by || '—'} · ${date(report.sealed_at)}`)));
}
