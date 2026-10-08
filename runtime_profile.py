"""One explicit inherited paper series; the scheduled legacy series stays default."""
import os
from pathlib import Path

INHERITED = 'a92-inherited-v1'
SOURCE_SHA = 'a92d79dad4fc21d38aab26a36f31f58a34b2dd62'


def independent() -> bool:
    name = os.environ.get('TRADE_SIMULATION_IDENTITY', '')
    if name not in ('', INHERITED):
        raise ValueError('UNKNOWN_SIMULATION_IDENTITY')
    return name == INHERITED


def state_branch() -> str:
    return 'runtime-state-' + INHERITED if independent() else 'runtime-state'


def report_path(day: str) -> str:
    return f'reports/{INHERITED}/{day}.md' if independent() else f'reports/{day}.md'


def source_path() -> Path:
    supplied = os.environ.get('TRADE_PREFETCHED_SOURCE', '') if independent() else ''
    return Path(supplied) if supplied else Path(os.environ['RUNNER_TEMP']) / 'trade-source'


def require_source(sha: str) -> None:
    if independent() and sha != SOURCE_SHA:
        raise ValueError('INDEPENDENT_SOURCE_IDENTITY')


def require_record(record: dict | None) -> None:
    if independent() and record is not None:
        if record.get('simulation_identity') != INHERITED or record.get('strategy_sha') != SOURCE_SHA:
            raise ValueError('INDEPENDENT_RECORD_IDENTITY')
        if 'report' in record:
            if (record['report'].get('simulation_identity') != INHERITED or
                    record['report'].get('strategy_sha') != SOURCE_SHA):
                raise ValueError('INDEPENDENT_REPORT_IDENTITY')
