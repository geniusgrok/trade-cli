"""Read existing evidence; never calculate or publish a new strategy result."""
import json, tarfile, io, csv
import private_store as store
day = '2026-09-30'
saved = store.request('result', {'date': day})
bundle = store.decode_bundle(saved['bundle'], saved['sha256'])
report = saved['report']
with tarfile.open(fileobj=io.BytesIO(bundle), mode='r:gz') as archive:
    files = {m.name: m for m in archive.getmembers() if m.isfile()}
    name = 'output/signals_' + day + '.json'
    native = json.load(archive.extractfile(files[name]))
    deployment = native['deployment']
    def route(value):
        return {key: value.get(key) for key in ('name','boundary','regime','leaders')}
    # Only public market diagnostics; no account, quantity, balance, setup log or key.
    print(json.dumps({
        'target_date': day, 'status': report['status'], 'strategy_sha': report['strategy_sha'],
        'profile_start': report['profile']['start_date'],
        'warmup_health': native['warmup_health'],
        'summary': native['summary'],
        'replay_decision': route(deployment['decision']),
        'current_decision': route(deployment['current_decision']),
    }, ensure_ascii=False))
    references = ('300308','300502','300394','688008','603986','002409','688072','688256','300054','688082','688300','688205','920045','300776','688535','688249','688347','300666','600206','688409','688361','300604','688120')
    start = report['profile']['start_date']
    for code in references:
        members = [n for n in files if n.endswith(code + '.csv') and 'snapshots/' in n]
        if not members:
            continue
        rows = list(csv.DictReader(io.TextIOWrapper(archive.extractfile(files[members[0]]),encoding='utf-8')))
        key = next((k for k in rows[0] if 'date' in k.lower() or k == ''), None) if rows else None
        dates = [r[key][:10] for r in rows] if key else []
        if dates and sum(d < start for d in dates) < 240:
            print(json.dumps({'reference_history':code,'first_date':min(dates),'last_date':max(dates),'pre_start_bars':sum(d < start for d in dates)},ensure_ascii=False))
