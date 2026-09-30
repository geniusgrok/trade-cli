"""Sparse checkout and locked installation with private diagnostics only."""
from __future__ import annotations
import base64
import os
from pathlib import Path
import subprocess
import sys
from datetime import datetime
from zoneinfo import ZoneInfo
import private_store as store


def main() -> int:
    root = Path(os.environ['RUNNER_TEMP'])
    source, runtime = root / 'trade-source', root / 'trade-runtime'
    logs = runtime / 'logs'; logs.mkdir(parents=True, exist_ok=True)
    token = os.environ.get('TRADE_READ_TOKEN', '')
    if not token:
        # Inspect only a boolean supplied by Actions, never a variable's value.
        variable_only = os.environ.get('SOURCE_TOKEN_VARIABLE_PRESENT', '').lower() == 'true'
        reason = 'SOURCE_TOKEN_IS_VARIABLE_NOT_SECRET' if variable_only else 'SOURCE_READ_SECRET_MISSING'
        try:
            store.request('event', {
                'date': datetime.now(ZoneInfo('Asia/Shanghai')).date().isoformat(),
                'status': 'PREPARATION_FAILED',
                'details': {'phase': 'SOURCE_CREDENTIAL', 'error': reason,
                            'strategy_calculation_started': False}})
        except Exception:
            print('PRIVATE_FAILURE_RECORD_UNAVAILABLE')
        print('BLOCKED: ' + reason)
        return 1
    if sys.version_info[:2] != (3, 12):
        print('BLOCKED: PYTHON_312_REQUIRED')
        return 1
    env = os.environ.copy()
    env['GIT_TERMINAL_PROMPT'] = '0'
    env['GIT_CONFIG_COUNT'] = '1'
    env['GIT_CONFIG_KEY_0'] = 'http.https://github.com/.extraheader'
    env['GIT_CONFIG_VALUE_0'] = 'AUTHORIZATION: basic ' + base64.b64encode(('x-access-token:' + token).encode()).decode()
    phase = 'SOURCE_CLONE'
    with (logs / 'setup.log').open('wb') as log:
        try:
            subprocess.run(['git', 'clone', '--depth=1', '--filter=blob:none', '--no-checkout', '--branch=main', 'https://github.com/geniusgrok/trade.git', str(source)], env=env, stdout=log, stderr=log, check=True, timeout=180)
            phase = 'SOURCE_SPARSE_CHECKOUT'
            subprocess.run(['git', 'sparse-checkout', 'set', '--no-cone', '/quantfusion/', '/data/trading_calendar.json', '/requirements*.txt', '/pyproject.toml', '/AGENTS.md', '/README.md'], cwd=source, env=env, stdout=log, stderr=log, check=True, timeout=180)
            phase = 'SOURCE_CHECKOUT'
            subprocess.run(['git', 'checkout', 'main'], cwd=source, env=env, stdout=log, stderr=log, check=True, timeout=180)
            phase = 'LOCK_VALIDATION'
            lock = source / 'requirements-lock.txt'
            if not lock.is_file():
                raise ValueError('PRODUCTION_LOCK_MISSING')
            phase = 'DEPENDENCY_INSTALLATION'
            install_env = os.environ.copy(); install_env.pop('TRADE_READ_TOKEN', None)
            subprocess.run([sys.executable, '-m', 'pip', 'install', '--disable-pip-version-check', '-r', str(lock)], cwd=source, env=install_env, stdout=log, stderr=log, check=True, timeout=360)
        except (subprocess.SubprocessError, OSError, ValueError) as error:
            log.flush()
            reason = 'TIMEOUT' if isinstance(error, subprocess.TimeoutExpired) else 'OPERATION_FAILED'
            try:
                tail = (logs / 'setup.log').read_text(errors='replace')[-16000:]
                tail = tail.replace(token, '[REDACTED]').replace(env['GIT_CONFIG_VALUE_0'], '[REDACTED]')
                if phase == 'SOURCE_CLONE':
                    lower_tail = tail.lower()
                    if 'repository not found' in lower_tail:
                        reason = 'SOURCE_REPOSITORY_NOT_ACCESSIBLE'
                    elif 'authentication failed' in lower_tail or 'invalid username or token' in lower_tail:
                        reason = 'SOURCE_AUTHENTICATION_FAILED'
                    elif 'could not resolve host' in lower_tail:
                        reason = 'SOURCE_DNS_FAILED'
                store.request('event', {'date': datetime.now(ZoneInfo('Asia/Shanghai')).date().isoformat(),
                    'status': 'PREPARATION_FAILED', 'details': {'phase': 'SOURCE_OR_ENVIRONMENT', 'operation': phase,
                    'reason': reason, 'log_tail': tail}})
            except Exception:
                pass
            print('BLOCKED: SOURCE_OR_ENVIRONMENT_PREPARATION_FAILED')
            print('PREPARATION_PHASE: ' + phase)
            print('PREPARATION_REASON: ' + reason)
            return 1
    print('SOURCE_AND_LOCKED_ENVIRONMENT_READY')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
