"""Run the existing production pipeline; own only scheduling and delivery."""
from __future__ import annotations
import bisect
import contextlib
from datetime import date, datetime, time, timedelta
import importlib.metadata
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time as clock
import traceback
import shutil
from zoneinfo import ZoneInfo

import private_store as store
import report as reporting
import runtime_profile as series

CHECKPOINT_SECTIONS = {'schema_version', 'last_completed_close', 'dates', 'initial_capital', 'engine_cfg', 'engine_policy',
    'effective_policy', 'account_risk_policy', 'engine', 'tail_policies', 'sleeves', 'portfolio_risk', 'run', 'overlay',
    'last_opinion', 'last_agreement', 'controller', 'budget'}


def identity(source: Path) -> dict:
    sha = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=source, text=True).strip()
    if not re.fullmatch('[0-9a-f]{40}', sha):
        raise ValueError('INVALID_SOURCE_SHA')
    series.require_source(sha)
    run = os.environ['GITHUB_RUN_ID']
    result = {'strategy_sha': sha, 'workflow_sha': os.environ['GITHUB_SHA'],
            'actions_run_id': run, 'actions_run_attempt': int(os.environ['GITHUB_RUN_ATTEMPT']),
            'actions_run_url': f'https://github.com/geniusgrok/trade-cli/actions/runs/{run}',
            'trigger': os.environ['GITHUB_EVENT_NAME'], 'started_at': datetime.now(ZoneInfo('Asia/Shanghai')).isoformat()}
    if series.independent():
        result['simulation_identity'] = series.INHERITED
    return result


def resolve_target(calendar, now: datetime, requested: str, event: str) -> tuple[str, str, str]:
    today = now.date().isoformat()
    target = requested or today
    if event == 'push' and not requested:
        # Deployment acceptance is explicitly the most recent completed session.
        i = bisect.bisect_right(calendar.sessions, today)
        if i and calendar.sessions[i - 1] == today and now.time() < time(15, 30):
            i -= 1
        if not i:
            raise ValueError('CALENDAR_OUT_OF_RANGE')
        target = calendar.sessions[i - 1]
    if not calendar.coverage_start <= target <= calendar.coverage_end or target > today:
        raise ValueError('CALENDAR_OUT_OF_RANGE')
    i = bisect.bisect_left(calendar.sessions, target)
    if not i:
        raise ValueError('PREVIOUS_SESSION_UNKNOWN')
    previous = calendar.sessions[i - 1]
    if target not in calendar.sessions:
        return target, previous, 'HOLIDAY'
    if target == today and now.time() < time(15, 30):
        return target, previous, 'BEFORE_CLOSE'
    return target, previous, 'READY'


def prepare_regime_inputs(data_dir, target: str, scan_dates: dict) -> dict:
    """Prepare live inputs using native providers without changing strategy code."""
    from quantfusion.data.contracts import refresh_regime_indices
    from quantfusion.data.sessions import index_coverage

    refresh = refresh_regime_indices(
        data_dir, end_date=target, strict=True, allow_provider_fallback=True
    )
    # A preserved file or a successful HTTP response is not date coverage.
    coverage = index_coverage(data_dir, scan_dates)
    return {'refresh': refresh, 'coverage': coverage}


def prepare_risk_inputs(ctx) -> dict:
    """Include the native independent risk basket in the immutable input snapshot."""
    from quantfusion.config.overlay import RISK_BASKET
    from quantfusion.data.providers import DataFetcher
    from quantfusion.data.sessions import require_frame_coverage

    start = (date.fromisoformat(ctx.request.start_date) - timedelta(days=400)).isoformat()
    additions, evidence_dates = {}, {}
    for code in RISK_BASKET:
        frame = ctx.snapshot_frames.get(code)
        if frame is None:
            frame = DataFetcher.load_stock_data(
                code, start, ctx.request.end_date,
                data_dir=getattr(ctx.request, 'local_market_dir', None), cache_dir=ctx.request.cache_dir
            )
            if getattr(ctx.request, 'local_market_dir', None) and frame is not None:
                frame = frame.loc[start:ctx.request.end_date].copy()
            additions[code] = frame
        if frame is None or frame.empty or frame.attrs.get('_stale', False):
            raise ValueError('RISK_INPUT_UNAVAILABLE:' + code)
        evidence_dates[code] = require_frame_coverage(frame, ctx.scan_dates, code)
    # Do not expand ctx.symbols or ctx.tradable: these are evidence, not candidates.
    ctx.snapshot_frames.update({code: frame.copy() for code, frame in additions.items()})
    ctx.actual_evidence_dates.update(evidence_dates)
    return evidence_dates


def current_bar_pending(message: str, target: str, previous_date: str, eligibility: dict) -> bool:
    """Only a proved one-session tail absence may wait; other errors stay errors."""
    grammar = r'TRADING_DAY_COVERAGE:(INDEX:)?([0-9]{6}): expected=([0-9-]{10}); observed=([0-9-]{10}); missing bar or conflicting/unverified suspension'
    matches = re.findall(grammar, message)
    if not matches or any(expected != target or observed != previous_date for _, _, expected, observed in matches):
        return False
    if any(not index and (eligibility.get(code, {}).get('status') != 'SESSION_REQUIRED' or
            eligibility[code].get('required_quote_date') != target) for index, code, _, _ in matches):
        return False
    # Native probe prints the complete fatal reasons again after its summary.
    reasons = re.findall(r'^\s+[0-9]{6} [^\n]+?: (.+)$', message, re.MULTILINE)
    if reasons and any(not re.fullmatch(r'TRADING_DAY_COVERAGE:[0-9]{6}: expected=' + re.escape(target) +
            r'; observed=' + re.escape(previous_date) + r'; missing bar or conflicting/unverified suspension', reason) for reason in reasons):
        return False
    if not reasons and re.fullmatch(r'(?:INDEX_EVIDENCE_UNAVAILABLE:[0-9]{6}: )?' + grammar, message.strip()) is None:
        return False
    return not any(marker in message for marker in ('INVALID_EVIDENCE', 'FUTURE_EVIDENCE', 'PROVIDER_STALE_OR_AGE',
        'history refresh failed', 'using cached data only', 'PRE_LISTING_EVIDENCE_CONFLICT', 'checkpoint', 'Unable to refresh index'))


def prepare_native_with_retry(build_context, load_risk, log, prepare, probe, pause=clock.sleep,
                              *, before=None, after=None, pending_session=None):
    """Wait for delayed index, stock and risk bars before the one daily replay."""
    for attempt in range(3):
        ctx = build_context()  # A failed probe may have partially populated its context.
        try:
            if before is not None:
                before(ctx)
            if not prepare(ctx, load_risk):
                break
            log.flush()
            start = log.tell()
            if probe(ctx):
                if after is not None:
                    after(ctx)
                return ctx
            log.flush()
            with Path(log.name).open('rb') as evidence:
                evidence.seek(start)
                message = evidence.read().decode('utf-8', errors='replace')
                coverage_pending = pending_session is not None and current_bar_pending(message, *pending_session,
                    getattr(ctx, 'scan_dates', {}).get('symbol_eligibility', {}))
        except (ValueError, RuntimeError) as exc:
            coverage_pending = pending_session is not None and current_bar_pending(str(exc), *pending_session,
                getattr(ctx, 'scan_dates', {}).get('symbol_eligibility', {}))
            if not coverage_pending:
                raise
            if attempt == 2:
                raise ValueError('CURRENT_BAR_UNAVAILABLE') from exc
        if coverage_pending and attempt == 2:
            raise ValueError('CURRENT_BAR_UNAVAILABLE')
        if not coverage_pending:
            break
        print('  当日行情覆盖尚未齐全，等待 10 分钟重新取数。')
        pause(600)
    raise ValueError('NATIVE_PREPARATION_FAILED')


def require_resume_interval(ctx, calendar, previous_close: str) -> dict:
    """Every unprocessed official session has evidence, with native event facts."""
    import pandas as pd
    from quantfusion.data.sessions import require_frame_coverage, stock_eligibility
    from quantfusion.data.contracts import _normalize_index_frame
    from quantfusion.config.overlay import RISK_BASKET
    from quantfusion.config.portfolio import PortfolioPolicy

    target = ctx.request.end_date
    sessions = [day for day in calendar.sessions if previous_close < day <= target]
    if previous_close not in calendar.sessions or not sessions or sessions[-1] != target:
        raise ValueError('RESUME_INTERVAL_INVALID')
    economic_codes = set(ctx.tradable) | set(PortfolioPolicy().regime_symbols)
    required = set(ctx.symbols) | set(RISK_BASKET) | set(PortfolioPolicy().regime_symbols)
    if not ctx.snapshot_frames or set(ctx.snapshot_frames) != required:
        raise ValueError('RESUME_FRAME_INVENTORY')
    frames = [(code, frame, False) for code, frame in ctx.snapshot_frames.items()]
    for code in ('000300', '000682'):
        frame = _normalize_index_frame(pd.read_csv(Path(ctx.request.regime_data_dir) / f'{code}.csv'), end_date=target)
        frame.index = pd.DatetimeIndex(frame['date'])
        frames.append(('INDEX:' + code, frame, True))
    economic_dates = set()
    for code, frame, index in frames:
        observed = [stamp.strftime('%Y-%m-%d') for stamp in frame.index]
        if any(day > target or (previous_close < day <= target and day not in sessions) for day in observed):
            raise ValueError('RESUME_UNEXPECTED_EVIDENCE_DATE')
        if code in economic_codes:
            economic_dates.update(day for day in observed if previous_close < day <= target)
        for day in sessions:
            dates = {**ctx.scan_dates, 'requested_as_of': day, 'required_evidence_date': day}
            prefix = frame.loc[:day]
            if not index and stock_eligibility(code, dates)['status'] == 'PRE_LISTING':
                if not prefix.empty:
                    raise ValueError('RESUME_PRE_LISTING_CONFLICT')
                continue
            try:
                require_frame_coverage(prefix, dates, code, allow_certified_suspension=not index)
            except ValueError as exc:
                if day != target:
                    raise ValueError('RESUME_HISTORY_GAP') from exc
                raise
    if economic_dates != set(sessions):
        raise ValueError('RESUME_ECONOMIC_SESSION_MISSING')
    return {'previous_close': previous_close, 'sessions': sessions, 'stock_count': len(ctx.snapshot_frames), 'index_count': 2}


def existing_result_outcome(existing: dict) -> str:
    if existing['status'] not in ('SUCCESS', 'DEGRADED'):
        raise ValueError('EXISTING_RESULT_UNUSABLE:' + existing['status'])
    return 'EXISTING_RESULT_' + existing['status']


def failure_reason(exc: Exception) -> str:
    """Public logs carry fixed categories; detailed evidence stays encrypted."""
    message = str(exc)
    if message == 'checkpoint observed input prefix changed (qfq vintage UNKNOWN)':
        return 'CHECKPOINT_HISTORY_CHANGED'
    if message == 'checkpoint source/runtime/config/calendar/pool identity changed':
        return 'CHECKPOINT_IDENTITY_CHANGED'
    code = message.split(':', 1)[0]
    allowed = {'ARCHIVED_SERIES_READ_ONLY', 'UNKNOWN_SIMULATION_IDENTITY', 'EXISTING_RESULT_UNUSABLE',
        'PREVIOUS_RUN_UNRESOLVED', 'COMPLETE_INHERITED_STATE_REQUIRED', 'COMPLETE_EXISTING_RESULT_REQUIRED',
        'COMPLETE_PUBLICATION_REQUIRED', 'COMPLETE_PUBLICATION_IDENTITY', 'COMPLETE_SNAPSHOT_REQUIRED',
        'INHERITED_SEED_REQUIRED', 'OUT_OF_ORDER_DATE', 'RESTORED_STATE_MISMATCH', 'RESTORED_CLOSE_DATE_MISMATCH',
        'CURRENT_BAR_UNAVAILABLE', 'NATIVE_PREPARATION_FAILED', 'RESUME_INTERVAL_INVALID', 'RESUME_FRAME_INVENTORY',
        'RESUME_UNEXPECTED_EVIDENCE_DATE', 'RESUME_PRE_LISTING_CONFLICT', 'RESUME_HISTORY_GAP',
        'RESUME_ECONOMIC_SESSION_MISSING', 'FROZEN_INDEX_IDENTITY_MISMATCH', 'BUNDLE_INTEGRITY', 'UNSAFE_BUNDLE'}
    return code if code in allowed else 'PRODUCTION_VALIDATION_FAILED'


def validate_native(native: dict, ctx, universe: dict, risk: dict | None) -> dict:
    target = ctx.request.end_date
    if native.get('status') != 'ok' or native.get('mode') != 'simulation' or native.get('scan_date') != target or native.get('run_id') != ctx.run_id:
        raise ValueError('NATIVE_RESULT_IDENTITY')
    actual = native.get('actual_evidence_dates', {})
    if any(actual.get(code) != target for code in universe):
        raise ValueError('STOCK_DATE_COVERAGE')
    from quantfusion.data.sessions import index_coverage
    indices = index_coverage(ctx.regime_dir, ctx.scan_dates)
    if not indices or any(v['evidence_date'] != target for v in indices.values()):
        raise ValueError('INDEX_DATE_COVERAGE')
    if native.get('warmup_health', {}).get('warmup_status') in ('INVALID', 'UNKNOWN', None):
        raise ValueError('WARMUP_INVALID')
    pointer = json.loads((Path(ctx.request.output_dir) / 'latest_success.json').read_text())
    if pointer.get('scan_date') != target or pointer.get('run_id') != ctx.run_id:
        raise ValueError('PUBLICATION_POINTER_IDENTITY')
    if native.get('risk_state_saved') and (not risk or risk.get('scan_date') != target):
        raise ValueError('RISK_STATE_DATE')
    return {'stock_evidence_dates': actual, 'index_evidence': indices,
            'scan_dates': ctx.scan_dates, 'risk_state_saved': native.get('risk_state_saved'),
            'snapshot_manifest_sha256': native.get('deployment', {}).get('snapshot_manifest_sha256'),
            'warmup_status': native.get('warmup_health', {}).get('warmup_status')}


def run() -> tuple[int, str]:
    series.require_writer()
    source = series.source_path()
    root = Path(os.environ['RUNNER_TEMP']) / 'trade-runtime'
    (root / 'logs').mkdir(parents=True, exist_ok=True)
    output = root / 'output'; output.mkdir(exist_ok=True)
    ident = identity(source)
    target = os.environ.get('TARGET_DATE', '') or datetime.now(ZoneInfo('Asia/Shanghai')).date().isoformat()
    reserved = False
    finalized = False
    report = {**ident, 'target_date': target, 'status': 'FAILED'}
    risk = None
    log_path = root / 'logs' / 'production.log'
    with log_path.open('w', encoding='utf-8') as log, contextlib.redirect_stdout(log), contextlib.redirect_stderr(log):
        try:
            sys.path.insert(0, str(source))
            from quantfusion.config import daily, universe
            from quantfusion.application.daily_scan import build_argument_parser
            from quantfusion.data.sessions import load_calendar, resolve_scan_dates
            from quantfusion.application.daily_context import ScanRequest, ScanContext
            from quantfusion.application.daily_input import prepare_scan, freeze_scan
            from quantfusion.application.daily_market import probe_market
            from quantfusion.application.daily_replay import run_simulation
            from quantfusion.application.daily_output import build_scan_artifact
            from quantfusion.application.daily_publication import publish_scan
            from quantfusion.io.state_store import load_prev_risk_state, save_risk_state, validate_risk_state, require_completed_publication

            calendar = load_calendar()
            target, previous_date, gate = resolve_target(calendar, datetime.now(ZoneInfo('Asia/Shanghai')), os.environ.get('TARGET_DATE', ''), ident['trigger'])
            report.update(target_date=target, previous_trading_date=previous_date)
            if gate != 'READY':
                store.request('event', {'date': target, 'status': gate, 'details': {**ident, 'previous_trading_date': previous_date,
                    'reason': '交易所日历休市' if gate == 'HOLIDAY' else '尚未完成收盘', 'calendar_sources': list(calendar.sources), 'calendar_sha256': calendar.sha256}})
                return 0, gate
            symbols = dict(universe.SYMBOL_NAMES)
            if tuple(symbols) != tuple(universe.ORDERED_SYMBOLS) or list(daily.SYMBOLS.items()) != list(symbols.items()) or len(symbols) != 17:
                raise ValueError('PRODUCTION_CORE17_IDENTITY')
            context = store.request('context', {'date': target, 'compare_date': previous_date})
            if context.get('existing'):
                saved = context['existing']
                outcome = existing_result_outcome(saved)
                from publish_report import verified_report
                bundle = store.decode_bundle(saved['bundle'], saved['sha256'])
                verified_report(saved, target, bundle)
                store.restore(bundle, root, complete=True)
                state = json.loads((output / 'risk_state.json').read_text())
                sections = state.get('checkpoint', {}).get('state', {})
                if (state != saved['risk_state'] or state.get('scan_date') != target or validate_risk_state(state) is not None or
                        set(sections) != CHECKPOINT_SECTIONS or type(sections.get('schema_version')) is not int or
                        sections['schema_version'] != 1):
                    raise ValueError('COMPLETE_EXISTING_RESULT_REQUIRED')
                require_completed_publication(str(output), state, target)
                from quantfusion.data.snapshot import verify_frozen_snapshot
                verify_frozen_snapshot(output / 'snapshots' / target)
                return 0, outcome
            previous = context.get('previous')
            if series.independent() and not previous:
                raise ValueError('INHERITED_SEED_REQUIRED')
            if series.independent():
                series.require_origin(previous.get('state_origin'))
                report['state_origin'] = previous['state_origin'].copy()
            if previous:
                bundle = store.decode_bundle(previous['bundle'], previous['bundle_sha256'])
                if series.independent():
                    store.restore(bundle, root, complete=True)
                else:
                    store.restore(bundle, root)
                state = json.loads((output / 'risk_state.json').read_text())
                if state != previous['risk_state']:
                    raise ValueError('RESTORED_STATE_MISMATCH')
                if series.independent():
                    if state['scan_date'] != previous['date']:
                        raise ValueError('RESTORED_CLOSE_DATE_MISMATCH')
                    sections = state.get('checkpoint', {}).get('state', {})
                    if (validate_risk_state(state) is not None or set(sections) != CHECKPOINT_SECTIONS or
                            type(sections.get('schema_version')) is not int or sections['schema_version'] != 1):
                        raise ValueError('COMPLETE_INHERITED_STATE_REQUIRED')
                    from quantfusion.data.snapshot import verify_frozen_snapshot
                    verify_frozen_snapshot(output / 'snapshots' / state['scan_date'])
            previous_profile = previous.get('profile', {}) if previous else {}
            start = previous_profile.get('start_date', daily.START_DATE)
            capital = previous_profile.get('capital', daily.INITIAL_CAPITAL)
            arguments = ['--start-date', start, '--end-date', target,
                '--capital', str(capital), '--cache-dir', str(root / 'cache'),
                '--regime-data-dir', str(root / 'regime'), '--output-dir', str(output)]
            local_market = os.environ.get('TRADE_LOCAL_MARKET_DIR', '') if series.independent() else ''
            if series.independent():
                arguments.append('--resume')
                report['continuation'] = {'mode': 'native_full_checkpoint_resume', 'previous_close': previous['date']}
            if local_market:
                shutil.copytree(Path(os.environ['TRADE_LOCAL_REGIME_DIR']), root / 'regime', dirs_exist_ok=True)
                arguments.extend(['--local-market-dir', local_market])
                import seed_contract
                provenance = seed_contract.strict_json((Path(local_market).parent / 'provenance.json').read_bytes())
                seed_contract.require_evidence_provenance(provenance)
                report['input_evidence'] = provenance
            args = build_argument_parser().parse_args(arguments)
            dates = resolve_scan_dates(target)
            def build_context():
                return ScanContext(ScanRequest.from_args(args, start, target, args.capital), symbols, dates)

            ctx = build_context()
            profile = {'start_date': start, 'capital': args.capital, 'config_fingerprint': ctx.config_fingerprint,
                       'universe': [[code, name] for code, name in symbols.items()]}
            report['profile'] = profile
            # Refresh after restoring prior evidence, before reserving computation.
            def before(ctx):
                report['phase'] = 'INDEX_PREPARATION'
                if local_market:
                    from quantfusion.data.sessions import index_coverage
                    report['index_preparation'] = {'mode': 'local_read_only', 'coverage': index_coverage(ctx.request.regime_data_dir, dates)}
                else:
                    report['index_preparation'] = prepare_regime_inputs(ctx.request.regime_data_dir, target, dates)
                report['phase'] = 'NATIVE_PREPARATION'

            def after(ctx):
                report['phase'] = 'RISK_INPUT_PREPARATION'
                report['risk_input_evidence_dates'] = prepare_risk_inputs(ctx)
                from quantfusion.data.sessions import index_coverage
                final_indices = index_coverage(ctx.request.regime_data_dir, dates)
                report['index_preparation']['final_refresh'] = ctx.index_refresh
                report['index_preparation']['final_coverage'] = final_indices
                report['resume_interval'] = require_resume_interval(ctx, calendar, previous['date'])
                if not freeze_scan(ctx):
                    raise ValueError('NATIVE_SNAPSHOT_FAILED')
                declared = {entry['path']: entry['sha256'] for entry in ctx.snapshot_manifest['evidence']}
                if any(declared.get('regime_data/' + code + '.csv') != facts['sha256'] for code, facts in final_indices.items()):
                    raise ValueError('FROZEN_INDEX_IDENTITY_MISMATCH')

            # Rebuild the complete evidence snapshot on each delayed-data retry.
            ctx = prepare_native_with_retry(build_context, load_prev_risk_state, log,
                                            prepare_scan, probe_market, before=before, after=after,
                pending_session=(target, previous_date) if target == datetime.now(ZoneInfo('Asia/Shanghai')).date().isoformat()
                    and previous['date'] == previous_date else None)
            if series.independent():
                from quantfusion.io.state_store import validate_checkpoint_identity
                validate_checkpoint_identity(ctx, ctx.prev_risk['checkpoint'])
            receipt = store.request('start', {'date': target, 'strategy_sha': ident['strategy_sha'],
                                             'previous_date': previous['date'] if previous else None})
            if not receipt.get('started'):
                raise ValueError('DUPLICATE_RESERVATION:' + receipt['status'])
            reserved = True
            report['phase'] = 'PRODUCTION_REPLAY'
            result, decision = run_simulation(ctx)
            report['phase'] = 'NATIVE_PUBLICATION'
            artifact = build_scan_artifact(ctx, result, decision)
            if artifact is None or publish_scan(ctx, result, artifact, save_risk_state) != 0:
                raise ValueError('NATIVE_PUBLICATION_FAILED')
            native_path = output / f'signals_{target}.json'
            native = json.loads(native_path.read_text())
            if native.get('risk_state_saved'):
                risk = json.loads((output / 'risk_state.json').read_text())
            validation = validate_native(native, ctx, symbols, risk)
            quotes = {}
            for code, frame in ctx.snapshot_frames.items():
                if code in symbols:
                    close = next((frame.iloc[-1][key] for key in ('close', 'Close') if key in frame.columns), None)
                    quotes[code] = {'date': str(frame.index[-1].date()), 'close': float(close) if close is not None else None}
            flags = native.get('summary', {})
            degraded = validation['warmup_status'] == 'DEGRADED' or not native.get('risk_state_saved') or flags.get('risk_state_identity_mismatch') or flags.get('current_route_mismatch')
            report.update(status='DEGRADED' if degraded else 'SUCCESS', data_date=target,
                          observation=reporting.observation(native, symbols, quotes), validation=validation,
                          dependencies={d: importlib.metadata.version(d) for d in ('numpy','pandas','akshare')})
            report['comparison'] = reporting.compare(report, context.get('comparison'), previous_date)
            report['phase'] = 'PRODUCTION_COMPLETED'
            report['finished_at'] = datetime.now(ZoneInfo('Asia/Shanghai')).isoformat()
            md = reporting.markdown(report)
            (output / 'daily_report.json').write_text(json.dumps(report, ensure_ascii=False, allow_nan=False), encoding='utf-8')
            (output / 'daily_report.md').write_text(md, encoding='utf-8')
            log.flush()
            proof = store.finish(target, report['status'], report, md, risk, root)
            finalized = True
            duplicate = store.request('start', {'date': target, 'strategy_sha': ident['strategy_sha'],
                'previous_date': previous['date'] if previous else None})
            if duplicate.get('started') is not False:
                raise ValueError('DAILY_DEDUP_NOT_VERIFIED')
            proof['daily_dedup_verified'] = True
            store.request('event', {'date': target, 'status': 'VERIFIED', 'details': {**ident, **proof, 'status': report['status'], 'members': len(symbols)}})
        except Exception as exc:
            traceback.print_exc()
            report['error'] = str(exc)
            report['finished_at'] = datetime.now(ZoneInfo('Asia/Shanghai')).isoformat()
            log.flush()
            if reserved and not finalized:
                # Never overwrite a successful uncertain publication with FAILED.
                existing = store.request('result', {'date': target})
                if existing and existing.get('status') != 'RUNNING':
                    raise RuntimeError('PUBLISHED_RESULT_REQUIRES_READBACK') from exc
                report['status'] = 'FAILED'
                store.finish(target, 'FAILED', report, reporting.markdown(report), None, root)
            else:
                store.request('event', {'date': target, 'status': 'DELIVERY_FAILED' if finalized else 'PREPARATION_FAILED',
                    'details': {**ident, 'error': str(exc), 'phase': report.get('phase', 'PREPARATION'), 'log_tail': log_path.read_text()[-16000:]}})
            return 1, 'PRIVATE_DAILY_TASK_FAILED:' + failure_reason(exc)
    return 0, 'VERIFIED_' + report['status']


if __name__ == '__main__':
    try:
        status, outcome = run()
    except Exception as exc:
        status, outcome = 1, 'PRIVATE_DAILY_TASK_FAILED:' + failure_reason(exc)
    # Never print reports, native diagnostics, tokens or private response bodies.
    print(outcome)
    raise SystemExit(status)
