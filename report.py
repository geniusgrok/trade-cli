"""Lossless presentation of native fields; no signal model or account inference."""
from __future__ import annotations
import json
from typing import Any

MISSING = '未提供'


def show(value: Any) -> str:
    if value is None:
        return MISSING
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False) if isinstance(value, (dict, list)) else str(value)


def cell(value: Any) -> str:
    return show(value).replace('|', '\\|').replace('\n', '<br>')


def observation(native: dict, universe: dict, quotes: dict) -> dict:
    rows = native.get('signals')
    if not isinstance(rows, list):
        raise ValueError('MISSING_NATIVE_ROWS')
    by_code = {row['code']: row for row in rows}
    if len(rows) != len(by_code) or set(by_code) != set(universe):
        raise ValueError('CORE17_COVERAGE')
    if native.get('symbols') != universe:
        raise ValueError('CORE17_IDENTITY')
    items = []
    for code, name in universe.items():
        items.append({'code': code, 'name': name, 'quote': quotes.get(code),
                      'native': by_code[code],
                      'pending_signals': [s for s in native.get('pending_signals', []) if s.get('symbol') == code],
                      'blocked_signals': [s for s in native.get('blocked_signals', []) if s.get('symbol') == code]})
    return {'market': {'current_decision': native.get('deployment', {}).get('current_decision'),
                       'risk_opinion': native.get('risk_opinion'),
                       'account_risk_budget': native.get('account_risk_budget'),
                       'warmup_health': native.get('warmup_health'),
                       'summary': native.get('summary'),
                       'portfolio': native.get('portfolio')}, 'symbols': items}


def _diff(old: Any, new: Any, path: str) -> list[dict]:
    if isinstance(old, dict) and isinstance(new, dict):
        changes = []
        for key in sorted(old.keys() | new.keys()):
            changes.extend(_diff(old.get(key), new.get(key), f'{path}.{key}'))
        return changes
    if old == new:
        return []
    return [{'field': path, 'yesterday': old, 'today': new}]


def compare(report: dict, previous: dict | None, previous_date: str) -> dict:
    if not previous or previous.get('target_date') != previous_date:
        return {'status': '不可比较', 'reason': '前一交易日结构化结果未取得或未核验', 'changes': []}
    if previous.get('profile') != report.get('profile'):
        return {'status': '不可比较', 'reason': '生产股票池顺序、固定起点、资金或配置口径变化', 'changes': []}
    old, new = previous.get('observation'), report.get('observation')
    if not isinstance(old, dict) or not isinstance(new, dict):
        return {'status': '不可比较', 'reason': '缺少可核验的逐票或市场字段', 'changes': []}
    changes = _diff(old.get('market'), new.get('market'), 'market')
    prior = {s['code']: s for s in old.get('symbols', [])}
    for symbol in new['symbols']:
        code = symbol['code']
        for key in ('native', 'pending_signals', 'blocked_signals'):
            changes.extend(_diff(prior.get(code, {}).get(key), symbol.get(key), f'{code}.{key}'))
    return {'status': '有变化' if changes else '无变化', 'changes': changes,
            'source_changed': previous.get('strategy_sha') != report.get('strategy_sha'),
            'previous_strategy_sha': previous.get('strategy_sha')}


def markdown(report: dict) -> str:
    out = ['# Trade Core17 盘后策略观察', '',
           f"状态：{report['status']}；目标交易日：{report['target_date']}；行情截止：{report.get('data_date', MISSING)}。",
           f"策略源码 SHA：`{report['strategy_sha']}`；[Actions Run]({report['actions_run_url']})。",
           f"运行来源：{report['trigger']}；执行口径：生产固定起点模拟，不代表真实账户持仓或成交。", '', '## 重点变化', '']
    comparison = report.get('comparison', {'status': '不可比较', 'reason': '没有合格结果'})
    out.append(f"对比日期：{report.get('previous_trading_date', MISSING)} → {report['target_date']}；{comparison['status']}。")
    if comparison.get('reason'):
        out.append(comparison['reason'])
    if comparison.get('source_changed'):
        out.append(f"源码版本变化：{comparison['previous_strategy_sha']} → {report['strategy_sha']}；不能把全部变化归因于行情。")
    changes = comparison.get('changes', [])
    for change in changes:
        out.append(f"- `{change['field']}`：{show(change['yesterday'])} → {show(change['today'])}")
    if report.get('error'):
        out.extend(['', '## 失败或限制', '', report['error']])
    if report.get('observation'):
        out.extend(['', '## 市场状态与市场风险（生产原始字段）', ''])
        for key, value in report['observation']['market'].items():
            out.extend([f'### {key}', '```json', show(value), '```'])
        out.extend(['', '## Core17 完整表（生产顺序）', '',
                    '| 代码 | 名称 | 行情日期 | 前复权收盘价 | 生产行动信号 | 模拟持有股数 | 资格 | 目标仓位 |',
                    '|---|---|---|---|---|---|---|---|'])
        for item in report['observation']['symbols']:
            row = item['native']; quote = item.get('quote') or {}
            out.append('| ' + ' | '.join(cell(v) for v in [item['code'], item['name'], quote.get('date'), quote.get('close'), row.get('signal'), row.get('held_shares'), row.get('eligibility'), row.get('target_weight')]) + ' |')
        out.extend(['', '## 逐票原始行动、资格、风险和仓位字段', ''])
        for item in report['observation']['symbols']:
            out.extend([f"### {item['code']} {item['name']}", '```json', show(item), '```'])
    out.extend(['', '## 数据、状态与证据', '', '```json', show(report.get('validation')), '```',
                '未输出字段为“未提供”。零数量或受阻候选不等于可执行买入；模拟持有不等于真实持仓。',
                '盘后观察仅供下一可交易日人工核对，不连接券商或下单。'])
    return '\n'.join(out) + '\n'
