"""One explicit inherited paper series; the scheduled legacy series stays default."""
import os
from pathlib import Path

INHERITED = 'a92-inherited-v1'
SOURCE_SHA = 'a92d79dad4fc21d38aab26a36f31f58a34b2dd62'
STATE_ORIGIN = {
    'kind': 'after_fact_reconstruction', 'baseline_close': '2026-09-30',
    'old_source': 'd2fee61a7f91679f6fdabaf97ba68cd030ad290a', 'cutover_source': SOURCE_SHA,
    'capture_sha256': 'd00707c2cc4f6ac17990bfde1c878cbb4523b77d9d7d1c544016837173706861',
    'original_record_sha256': '6c948b8273fa8a8d9155ccf350ab8fbd96e02c9c83bf19384e1e1d2ab72c97ac',
    'original_bundle_sha256': 'e9aa0cd6da700515f19e4e71d9e19ef645b8447ab692fe30ea6fc9115d0e8b0f',
    'adaptation_proof_sha256': '004970145c58e8b8f077f5bff28eebf7976c74fad7a4b7370494814fc2cbac31',
    'admission_change': 'none_to_ordinary_systemic_admission',
}


def require_origin(value: dict) -> None:
    if value != STATE_ORIGIN:
        raise ValueError('INDEPENDENT_STATE_ORIGIN')


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
        require_origin(record.get('state_origin'))
        if 'report' in record:
            if (record['report'].get('simulation_identity') != INHERITED or
                    record['report'].get('strategy_sha') != SOURCE_SHA):
                raise ValueError('INDEPENDENT_REPORT_IDENTITY')
            require_origin(record['report'].get('state_origin'))
