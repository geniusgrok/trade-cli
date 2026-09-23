"""中文公开展示边界；未知字段不自动公开，不推导交易信号。"""
from __future__ import annotations

from datetime import date
import math
from pathlib import Path
import re
from typing import Any

MISSING = '未提供'
STATUS = {'SUCCESS': '成功', 'DEGRADED': '降级', 'FAILED': '失败'}
# Exact native paths are the publication contract, not a recursive object dump.
MARKET = {
    'current_decision.name': '当前部署状态',
    'current_decision.regime.regime': '市场环境',
    'current_decision.regime.health.state': '市场数据状态',
    'current_decision.leaders.health.state': '龙头数据状态',
    'current_decision.leaders.selected_symbols': '当前入选标的',
    'risk_opinion.date': '风险意见日期',
    'risk_opinion.regime': '风险环境',
    'risk_opinion.risk_level': '风险等级',
    'risk_opinion.risk_confidence': '风险置信度',
    'risk_opinion.bull_silent': '多头静默状态',
    'risk_opinion.sleeve_consensus': '策略一致度',
    'risk_opinion.sleeve_consensus_decline_streak': '策略一致度连续下降次数',
    'risk_opinion.weakest_clusters': '最弱分组',
    'risk_opinion.block_new_entries': '独立风险意见禁止新开仓',
    'risk_opinion.block_pyramids': '独立风险意见禁止加仓',
    'risk_opinion.recommended_gross_cap': '建议总仓位上限',
    'risk_opinion.reason_codes': '风险原因代码',
    'risk_opinion.coverage.observed': '风险篮已覆盖数量',
    'risk_opinion.coverage.total_basket': '风险篮应覆盖数量',
    'risk_opinion.coverage.observed_ratio': '风险篮覆盖比例',
    'risk_opinion.coverage.confidence': '风险篮置信度',
    'risk_opinion.coverage.observed_industries': '已覆盖行业数量',
    'risk_opinion.coverage.total_industries': '应覆盖行业数量',
    'warmup_health.warmup_status': '预热状态',
    'warmup_health.reasons': '预热限制原因',
    'warmup_health.indicator_ready_ratio': '指标就绪比例',
    'warmup_health.reference_basket_ready_ratio': '参考篮就绪比例',
    'warmup_health.regime_index_ready': '路由指数就绪',
    'warmup_health.stale_symbols': '行情过期标的',
    'summary.buys_suppressed': '最终禁止买入',
    'summary.current_route_mismatch': '当前路由不一致',
    'summary.risk_state_identity_mismatch': '风险状态身份不一致',
    'summary.buy': '买入信号数量',
    'summary.sell': '卖出信号数量',
    'summary.hold': '持有信号数量',
    'summary.wait': '观望信号数量',
    'summary.untradeable': '不可交易数量',
    'portfolio.safe_mode_active': '安全模式',
    'portfolio.sector_guard_active': '行业保护',
    'portfolio.terminal_risk_lock': '终止风险锁',
}
SIGNAL = {
    'signal': '行动信号', 'action': '行动', 'side': '方向',
    'eligibility': '资格', 'target_weight': '目标仓位',
    'risk_level': '风险等级', 'risk_restrictions': '风险限制',
    'reason_codes': '原因代码', 'strategies': '策略信号',
}
EXPLANATIONS = {
    'positive_momentum_hold': '正动量持有', 'choppy': '震荡', 'trend': '趋势',
    'READY': '就绪', 'DEGRADED': '降级', 'INVALID': '无效', 'UNKNOWN': '未知',
    'reference_basket_incomplete': '参考篮数据不完整',
}
BLOCKED = re.compile(
    r'(?i)(?:[A-Z][A-Z0-9_]*(?:TOKEN|SECRET|PASSWORD|CREDENTIAL|API_KEY)[A-Z0-9_]*'
    r'|\b(?:gh[pousr]_[A-Za-z0-9]+|github_pat_[A-Za-z0-9_]+|sk-[A-Za-z0-9]+)'
    r'|-----BEGIN[^\n]*PRIVATE KEY|\bBearer\s+\S+|\bAuthorization\s*:'
    r'|[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}|https?://[^\s<>]*supabase\.'
    r'|\beyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+'
    r'|/home/runner/|/mnt/data/|ychenracing/trade|ykhdfyjbfdvayqmvaxgb)'
)


def safe_document(text: str) -> None:
    if BLOCKED.search(text):
        raise ValueError('PUBLIC_DOCUMENT_REJECTED')
    # Only the public runner and local documentation links are permitted.
    for url in re.findall(r'https?://[^\s)<>]+', text):
        if not re.fullmatch(r'https://github\.com/geniusgrok/trade-cli(?:/[A-Za-z0-9_./-]*)?', url):
            raise ValueError('PUBLIC_LINK_REJECTED')


def checked_date(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
        raise ValueError('INVALID_REPORT_DATE')
    date.fromisoformat(value)
    return value


def get(value: Any, path: str) -> Any:
    for key in path.split('.'):
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def scalar(value: Any) -> str:
    if value is None:
        return MISSING
    if isinstance(value, bool):
        return '是' if value else '否'
    if isinstance(value, (float, int)):
        if not math.isfinite(value):
            raise ValueError('INVALID_PUBLIC_NUMBER')
        return str(value)
    if isinstance(value, list):
        if any(isinstance(v, (dict, list)) for v in value):
            raise ValueError('UNEXPECTED_PUBLIC_STRUCTURE')
        return '、'.join(scalar(v) for v in value) if value else '无记录'
    if not isinstance(value, str) or len(value) > 500 or any(ord(c) < 32 for c in value):
        raise ValueError('INVALID_PUBLIC_VALUE')
    safe_document(value)
    text = f'{EXPLANATIONS[value]}（{value}）' if value in EXPLANATIONS else value
    return text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;').replace('|', '\\|').replace('`', '\\`')


def intents(values: Any) -> str:
    if values is None:
        return MISSING
    if not isinstance(values, list) or any(not isinstance(v, dict) for v in values):
        raise ValueError('INVALID_PUBLIC_SIGNALS')
    rows = ['；'.join(f'{label}：{scalar(row[key])}' for key, label in SIGNAL.items() if key in row)
            for row in values]
    return ' / '.join(row or '存在记录，未提供可公开的行动字段' for row in rows) if rows else '无记录'



def symbol_names(report: dict) -> dict[str, str]:
    items = get(report, 'observation.symbols') or []
    return {item['code']: item['name'] for item in items
            if isinstance(item, dict) and isinstance(item.get('code'), str)
            and isinstance(item.get('name'), str) and item['name']}


def with_symbol_names(value: Any, names: dict[str, str]) -> str:
    text = scalar(value)
    return re.sub(r'(?<!\d)(\d{6})(?!\d)',
                  lambda match: f"{match.group(1)} {names[match.group(1)]}"
                  if match.group(1) in names else match.group(0), text)


def changes(report: dict) -> list[str]:
    comparison = report.get('comparison') or {}
    if comparison.get('status') == '不可比较':
        return []
    labels = {'market.' + path: label for path, label in MARKET.items()}
    formats = {}
    for item in get(report, 'observation.symbols') or []:
        code, name = item['code'], item['name']
        for field, label in SIGNAL.items():
            labels[f'{code}.native.{field}'] = f'{code} {scalar(name)}·{label}'
        for field, label in [('pending_signals', '待执行意图'), ('blocked_signals', '受阻意图')]:
            path = f'{code}.{field}'
            labels[path] = f'{code} {scalar(name)}·{label}'
            formats[path] = intents
    result = []
    for change in comparison.get('changes', []):
        path = change.get('field')
        if path not in labels:
            continue
        fmt = formats.get(path, scalar)
        old, new = fmt(change.get('yesterday')), fmt(change.get('today'))
        if old != new:
            names = symbol_names(report)
            result.append(f'{with_symbol_names(labels[path], names)}：{with_symbol_names(old, names)} → {with_symbol_names(new, names)}')
    return result


def markdown(report: dict) -> str:
    target = checked_date(report['target_date'])
    status = report['status']
    if status not in STATUS:
        raise ValueError('UNFINISHED_REPORT')
    sha = report['strategy_sha']
    run_id = str(report['actions_run_id'])
    if not re.fullmatch('[0-9a-f]{40}', sha) or not run_id.isdigit():
        raise ValueError('INVALID_PUBLIC_IDENTITY')
    previous = report.get('previous_trading_date')
    if previous is not None:
        checked_date(previous)
    data_date = report.get('data_date')
    if data_date is not None:
        checked_date(data_date)
    out = ['# Trade Core17 盘后日报', '',
           f'目标交易日：{target}；行情截止日：{data_date or MISSING}。',
           f'策略结果状态：**{STATUS[status]}（{status}）**。',
           f'实际生产源码版本：`{sha}`。',
           f'[查看本次计算的运行记录](https://github.com/geniusgrok/trade-cli/actions/runs/{run_id})。',
           '执行口径：生产固定起点模拟，仅作策略观察，不代表真实账户持仓或成交。', '', '## 重点变化', '']
    comparison = report.get('comparison') or {}
    comparable = comparison.get('status') in {'有变化', '无变化'}
    out.append(f'比较日期：{previous or MISSING} → {target}。')
    delta = changes(report) if comparable else []
    out.extend(('- ' + row for row in delta) if delta else [
        '公开信号字段无变化。' if comparable else '不可比较：前一交易日结果缺失、未核验或比较口径不一致。'])
    if comparison.get('source_changed'):
        out.append('生产源码版本发生变化，不能将全部信号变化归因于行情。')
    if status != 'SUCCESS':
        out.extend(['', '## 结果限制', '', '本次结果降级或失败，不能作为完全正常的决策输出；下文保留实际状态，不推断缺失信号。'])
    observation = report.get('observation')
    if observation:
        items = observation['symbols']
        codes = [item['code'] for item in items]
        if len(codes) != 17 or len(set(codes)) != 17 or any(not re.fullmatch(r'\d{6}', c) for c in codes):
            raise ValueError('PUBLIC_CORE17_COVERAGE')
        out.extend(['', '## 市场状态与市场风险', '', '| 项目 | 生产输出 |', '|---|---|'])
        names = symbol_names(report)
        if set(names) != set(codes):
            raise ValueError('PUBLIC_CORE17_NAMES')
        for path, label in MARKET.items():
            value = with_symbol_names(get(observation.get("market"), path), names)
            out.append(f'| {label} | {value} |')
        out.extend(['', '独立风险意见与最终买入限制须结合阅读；风险等级为零不等于允许买入。', '',
                    '## Core17 全部标的（生产顺序）', '',
                    '| 代码 | 名称 | 行情日期 | 前复权收盘价 | 行动信号 | 资格 | 目标仓位 | 风险等级 |',
                    '|---|---|---|---|---|---|---|---|'])
        for item in items:
            native, quote = item.get('native') or {}, item.get('quote') or {}
            out.append('| ' + ' | '.join(scalar(v) for v in [item['code'], item['name'], quote.get('date'), quote.get('close'),
                native.get('signal'), native.get('eligibility'), native.get('target_weight'), native.get('risk_level')]) + ' |')
        out.extend(['', '## 逐票行动与风险补充', '', '| 代码 | 风险限制 | 原因代码 | 策略信号 | 待执行意图 | 受阻意图 |', '|---|---|---|---|---|---|'])
        for item in items:
            native = item.get('native') or {}
            out.append('| ' + ' | '.join([item['code'], scalar(native.get('risk_restrictions')), scalar(native.get('reason_codes')),
                scalar(native.get('strategies')), intents(item.get('pending_signals')), intents(item.get('blocked_signals'))]) + ' |')
    else:
        out.extend(['', '未取得完整、可展示的逐票结果，不补造信号。'])
    out.extend(['', '## 阅读说明', '', '未提供的字段不解释为零、无风险或无变化。原始枚举保留，中文说明不替代生产输出。',
                '公开日报不包含账户资金、持仓数量、订单标识或内部运行明细；目标仓位仅在生产明确输出时展示。',
                '零数量或受阻候选不等于可执行买入；待执行意图不代表已成交。',
                '补存或再次读取日报不会重新计算策略，不改变原结果的日期、版本和状态。',
                '盘后信号仅供下一可交易日人工核对，不连接券商或下单。'])
    result = '\n'.join(out) + '\n'
    safe_document(result)
    return result


def check_docs(root: Path) -> int:
    count = 0
    for path in root.rglob('*'):
        if any(part in {'.git', '.venv', 'venv', '__pycache__', '.pytest_cache'} for part in path.relative_to(root).parts):
            continue
        if path.is_file() and path.suffix.lower() in {'.md', '.markdown', '.rst'}:
            safe_document(path.read_text(encoding='utf-8'))
            count += 1
    return count


if __name__ == '__main__':
    check_docs(Path('.'))
    print('公开文档检查通过')
