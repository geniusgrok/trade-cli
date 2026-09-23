export const AUDIENCE = 'https://ykhdfyjbfdvayqmvaxgb.supabase.co/functions/v1/trade-daily';
export function authorize(p) {
  const expected = {
    repository: 'geniusgrok/trade-cli', repository_id: '1382522505',
    repository_owner: 'geniusgrok', repository_owner_id: '332557824',
    ref: 'refs/heads/main',
    sub: 'repo:geniusgrok/trade-cli:ref:refs/heads/main',
    workflow_ref: 'geniusgrok/trade-cli/.github/workflows/trade-daily.yml@refs/heads/main',
    runner_environment: 'github-hosted',
  };
  for (const [key, value] of Object.entries(expected)) {
    if (p[key] !== value) throw new Error('UNAUTHORIZED_WORKFLOW');
  }
  if (!['schedule', 'workflow_dispatch', 'push'].includes(p.event_name) ||
      !/^[0-9]+$/.test(p.run_id ?? '') || !/^[1-9][0-9]*$/.test(p.run_attempt ?? '') ||
      !/^[0-9a-f]{40}$/.test(p.sha ?? '')) throw new Error('INVALID_RUN_IDENTITY');
  return {run: p.run_id, attempt: Number(p.run_attempt), sha: p.sha};
}
