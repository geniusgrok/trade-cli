"""Express complete refreshed qfq histories in the committed paper price basis."""
from __future__ import annotations

from decimal import Decimal, ROUND_HALF_EVEN
import hashlib
import json
from pathlib import Path
import sys


def prefix_hash(frame, close: str) -> str:
    """Match the producer's parsed CSV fingerprint, including numeric dtypes."""
    import io
    import pandas as pd
    persisted = frame.copy()
    persisted.index.name = 'date'
    parsed = pd.read_csv(io.StringIO(persisted.to_csv()), float_precision='round_trip')
    dates = pd.to_datetime(parsed['date'], errors='raise')
    prefix = parsed.loc[dates <= pd.Timestamp(close)].copy()
    prefix['date'] = dates.loc[prefix.index].dt.strftime('%Y-%m-%d')
    return hashlib.sha256(prefix.to_csv(index=False, float_format='%.17g').encode()).hexdigest()


def match_price_basis(current, committed, close: str, expected: str):
    """Accept a single scale only when it reproduces every committed input bit.

    No old rows are copied into the new frame, and no fingerprint is rewritten.
    Cent/mill rounding follows the precision actually retained by the producer.
    A gap, volume revision, or nonuniform price revision remains an error.
    """
    from quantfusion.data.providers import _normalize_columns
    prices = ('open', 'close', 'high', 'low')
    old, new = committed.loc[:close], current.loc[:close]
    if (old.empty or not old.index.equals(new.index) or list(old.columns) != list(new.columns)
            or not old['volume'].equals(new['volume'])):
        raise ValueError('CHECKPOINT_HISTORY_CHANGED')
    old_values = [Decimal(str(value)) for value in old[list(prices)].to_numpy().ravel()]
    new_values = [Decimal(str(value)) for value in new[list(prices)].to_numpy().ravel()]
    if any(not value.is_finite() or value <= 0 for value in old_values + new_values):
        raise ValueError('CHECKPOINT_HISTORY_CHANGED')
    digits = max(0, max(-value.normalize().as_tuple().exponent for value in old_values))
    if digits > 6:
        raise ValueError('CHECKPOINT_HISTORY_CHANGED')
    quantum = Decimal(1).scaleb(-digits)
    lower = max((old - quantum / 2) / new for old, new in zip(old_values, new_values))
    upper = min((old + quantum / 2) / new for old, new in zip(old_values, new_values))
    if lower >= upper:
        raise ValueError('CHECKPOINT_HISTORY_CHANGED')
    scale = (lower + upper) / 2
    if scale == 1:
        raise ValueError('CHECKPOINT_HISTORY_CHANGED')
    adjusted = current.copy()
    for name in prices:
        adjusted[name] = [float((Decimal(str(value)) * scale).quantize(quantum, rounding=ROUND_HALF_EVEN))
                          for value in current[name]]
    adjusted = _normalize_columns(adjusted)
    if prefix_hash(adjusted, close) != expected:
        raise ValueError('CHECKPOINT_HISTORY_CHANGED')
    return adjusted, {'scale': str(scale), 'price_decimals': digits,
                      'committed_prefix_sha256': expected, 'exact_prefix_verified': True}


def prepare_continuous_inputs(ctx, previous_snapshot: Path) -> dict:
    """Verify the old snapshot and normalize complete frames before freezing."""
    import pandas as pd
    from quantfusion.io.state_store import checkpoint_prefix_hashes
    from quantfusion.data.providers import _normalize_columns

    checkpoint = ctx.prev_risk['checkpoint']
    close = checkpoint['state']['last_completed_close']
    expected = checkpoint['prefix_hashes']
    if checkpoint_prefix_hashes(previous_snapshot, close) != expected:
        raise ValueError('RESTORED_STATE_MISMATCH')
    proof, replacements = {}, {}
    raw = Path(ctx.request.output_dir).parent / 'input_basis'
    for code, frame in ctx.snapshot_frames.items():
        name = 'market_data/' + code + '.csv'
        if name not in expected:
            raise ValueError('RESUME_FRAME_INVENTORY')
        if prefix_hash(frame, close) == expected[name]:
            continue
        committed = _normalize_columns(pd.read_csv(previous_snapshot / name, float_precision='round_trip'))
        try:
            adjusted, facts = match_price_basis(frame, committed, close, expected[name])
        except ValueError:
            before, after = committed.loc[:close], frame.loc[:close]
            common = before.index.intersection(after.index)
            samples = list(common[:2]) + list(common[-2:])
            details = {'symbol': code, 'provider': frame.attrs.get('volume_provider', 'unknown'),
                'volume_dtype': [str(before['volume'].dtype), str(after['volume'].dtype)],
                'volume_equal': before['volume'].equals(after['volume']),
                'price_decimals': max(-Decimal(str(v)).normalize().as_tuple().exponent
                    for v in before[['open', 'close', 'high', 'low']].to_numpy().ravel()),
                'samples': [{'date': str(day.date()), 'committed': float(before.loc[day, 'close']),
                            'upstream': float(after.loc[day, 'close'])} for day in samples]}
            print('PRICE_BASIS_REJECTED:' + json.dumps(details, sort_keys=True), file=sys.__stdout__, flush=True)
            raise
        # Retain the complete upstream history and the conversion receipt.
        raw.mkdir(parents=True, exist_ok=True)
        path = raw / f'{code}.csv'
        frame.to_csv(path, index=True)
        facts['upstream_sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
        facts['upstream_close'] = float(frame['close'].iloc[-1])
        facts['simulation_close'] = float(adjusted['close'].iloc[-1])
        facts['previous_close'] = close
        facts['target_date'] = ctx.request.end_date
        proof[code] = facts
        replacements[code] = adjusted
    ctx.snapshot_frames.update(replacements)
    return proof
