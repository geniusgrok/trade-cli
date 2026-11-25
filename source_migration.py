"""One approved source transition; never rebuild or edit the prior paper ledger."""
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

import runtime_profile as profile

# These are native package fingerprints, not Git commit or bundle digests.
OLD_NATIVE_SOURCE = '8dd54caf07e284cf64a3e2091a5c300439fa1958a87896973cf7e660d88bc3be'
NEW_NATIVE_SOURCE = 'c9e80ae6df1b803f14c50e88f3ea2454401ca5446637a728a91da94d58735533'
SELL_FIELDS = {'sell_parent_shares', 'sell_position_shares'}


def _star_declaration(signal: dict, sleeve: dict, state: dict) -> tuple[int | None, int | None]:
    """Recover an actual earlier declaration, never fabricate a sell parent.

    Legacy a92 allowed old STAR declarations below the new minimum. A still
    unfilled illegal declaration is passed unchanged to the new open
    normalizer (which can reject or cap it); a proved partial fill of a legal
    parent retains that parent. The ledger/queue is not changed at migration.
    """
    from quantfusion.domain.rules import sell_order_quantity
    symbol, strategy = signal['symbol'], signal['strategy_name']
    remaining = signal['target_shares']
    day = signal['signal_date']
    current = sum(p['shares'] for p in sleeve['positions']
                  if (p['symbol'], p['strategy_name']) == (symbol, strategy))
    if not current:
        return None, None  # Native open handles an already absent position.
    if day == state['last_completed_close']:
        parent, balance = remaining, current
    else:
        # The old final-close journal records the actual original queue, in
        # sleeve-qualified strategy names. Its original quantity is evidence.
        decorated = {**signal, 'strategy_name': f"{sleeve['name']}:{strategy}"}
        rows = [q for entry in state['controller']['values']['journal'] if entry['date'] == day
                for q in entry.get('pending_signals', [])
                if {k: v for k, v in q.items() if k != 'target_shares'} ==
                   {k: v for k, v in decorated.items() if k != 'target_shares'}]
        if len(rows) != 1:
            # A legal residual can be redeclared at its own size; an odd
            # partial residual cannot acquire a larger fictional parent.
            if sell_order_quantity(symbol, remaining, current) == remaining:
                return remaining, current
            raise ValueError('CHECKPOINT_MIGRATION_SELL_PROOF')
        parent = rows[0]['target_shares']
        trades = [t for t in sleeve['trades']
                  if (t['symbol'], t['strategy_name']) == (symbol, strategy)]
        def shares_at(close):
            return sum(t['shares'] if t['direction'] == 'buy' else -t['shares']
                       for t in trades if t['date'] <= close)
        balance = shares_at(day)
        matched = sum(t['shares'] for t in trades if t['direction'] == 'sell'
                      and t['date'] > day and
                      all(t[k] == signal[k] for k in ('signal_date', 'reason', 'purpose',
                              'risk_reason', 'risk_scope', 'fusion_votes')))
        if shares_at(state['last_completed_close']) != current or parent - matched != remaining or balance <= 0:
            raise ValueError('CHECKPOINT_MIGRATION_SELL_PROOF')
    if remaining > parent or remaining <= 0:
        raise ValueError('CHECKPOINT_MIGRATION_SELL_PROOF')
    return (parent, balance) if sell_order_quantity(symbol, parent, balance) == parent else (None, None)


def _guard_prefix(sleeve: dict, state: dict, request, previous_snapshot: Path):
    """Use the same native loader and warm fixed-basket dates as the guard."""
    import pandas as pd
    from quantfusion.data.providers import load_stock_data
    start = pd.Timestamp(request.start_date) - pd.Timedelta(days=request.warmup_days)
    first = pd.Timestamp(state['dates'][0])
    codes = sleeve['policy']['regime_symbols']
    prefix = set()
    for code in codes:
        frame = load_stock_data(code, str(start.date()), state['last_completed_close'],
                                data_dir=str(previous_snapshot / 'market_data'))
        prefix.update(day for day in frame.index if start <= day < first)
    return len(prefix)


def migrate_a92_checkpoint(ctx, previous: dict, previous_snapshot: Path) -> dict:
    """Verify the immutable old close, migrate representation, restore it natively.

    Only the source fingerprint, two new Signal context fields and guard
    calendar coordinates change. No prior dates are evaluated under new rules.
    Everything remains in memory until the next native close is committed.
    """
    from quantfusion.application.daily_context import ScanContext
    from quantfusion.application.daily_replay import run_simulation
    from quantfusion.data.sessions import resolve_scan_dates
    from quantfusion.domain.models import Signal
    from quantfusion.io.artifacts import normalize_json
    from quantfusion.io.state_store import (
        build_checkpoint_identity, checkpoint_digest, checkpoint_prefix_hashes,
        require_completed_publication, validate_checkpoint_identity, validate_risk_state,
    )

    original = ctx.prev_risk
    if (previous.get('strategy_sha') != profile.SEED_SOURCE_SHA or
            previous.get('risk_state') != original or not original or
            previous.get('date') != original.get('scan_date') or
            validate_risk_state(original) is not None):
        raise ValueError('CHECKPOINT_MIGRATION_SOURCE')
    profile.require_origin(previous.get('state_origin'))
    old = original['checkpoint']
    close = old['state']['last_completed_close']
    if close >= ctx.request.end_date:
        raise ValueError('CHECKPOINT_MIGRATION_BOUNDARY')
    require_completed_publication(ctx.request.output_dir, original, ctx.request.end_date)
    new_identity = normalize_json(build_checkpoint_identity(ctx))
    if (new_identity.get('source_sha256') != NEW_NATIVE_SOURCE or
            old['identity'] != {**new_identity, 'source_sha256': OLD_NATIVE_SOURCE}):
        raise ValueError('CHECKPOINT_MIGRATION_IDENTITY')
    expected = old['prefix_hashes']
    if checkpoint_prefix_hashes(previous_snapshot, close) != expected:
        raise ValueError('RESTORED_STATE_MISMATCH')
    if checkpoint_prefix_hashes(ctx.snapshot_dir, close) != expected:
        raise ValueError('CHECKPOINT_HISTORY_CHANGED')

    migrated = deepcopy(original)
    checkpoint = migrated['checkpoint']
    state = checkpoint['state']
    old_signal_fields = set(Signal.__dataclass_fields__) - SELL_FIELDS
    offset_by_sleeve, pending_count, legacy_revalidation = {}, 0, 0
    for sleeve in state['sleeves']:
        offset = _guard_prefix(sleeve, state, ctx.request, previous_snapshot)
        points = sleeve['values']['_sector_shock_positions']
        if any(type(pos) is not int or not 0 <= pos < len(state['dates']) for pos in points):
            raise ValueError('CHECKPOINT_MIGRATION_GUARD')
        sleeve['values']['_sector_shock_positions'] = [pos + offset for pos in points]
        offset_by_sleeve[sleeve['name']] = offset
        for item in sleeve['pending']:
            signal = item['signal']
            if set(signal) != old_signal_fields:
                raise ValueError('CHECKPOINT_MIGRATION_SIGNAL')
            parent = balance = None
            if signal['direction'] == 'sell' and signal['symbol'].startswith('68'):
                parent, balance = _star_declaration(signal, sleeve, state)
                legacy_revalidation += int(parent is None)
            signal.update(sell_parent_shares=parent, sell_position_shares=balance)
            pending_count += 1
    checkpoint['identity'] = new_identity
    checkpoint['payload_sha256'] = checkpoint_digest(checkpoint)
    if validate_risk_state(migrated) is not None:
        raise ValueError('CHECKPOINT_MIGRATION_STATE')

    # A no-new-close native restore proves exact ownership, positions, marks,
    # pending intent, route prefix, locks, and all historical economic state.
    # It never publishes, calls providers, or reserves a business run.
    request = replace(ctx.request, end_date=close)
    probe = ScanContext(request, ctx.symbols, resolve_scan_dates(close, calendar_file=request.calendar_file))
    probe.tradable, probe.prev_risk, probe.snapshot_dir = ctx.tradable, migrated, previous_snapshot
    run_simulation(probe)
    if probe.checkpoint is None or probe.checkpoint['state'] != state:
        raise ValueError('CHECKPOINT_MIGRATION_RESTORE')
    validate_checkpoint_identity(ctx, checkpoint)
    ctx.prev_risk = migrated
    return {
        'from_source': profile.SEED_SOURCE_SHA, 'to_source': profile.SOURCE_SHA,
        'previous_close': close, 'old_payload_sha256': old['payload_sha256'],
        'adapted_payload_sha256': checkpoint['payload_sha256'],
        'guard_calendar_prefix': offset_by_sleeve, 'pending_carried': pending_count,
        'legacy_star_revalidation': legacy_revalidation, 'restored_state_exact': True,
    }
