import { createRemoteJWKSet, jwtVerify } from 'npm:jose@6.2.5';
import { AUDIENCE, authorize } from './auth.mjs';
const keys = createRemoteJWKSet(new URL('https://token.actions.githubusercontent.com/.well-known/jwks'));
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: {'Content-Type':'application/json','Cache-Control':'no-store'} });
async function rpc(name: string, args: Record<string, unknown>) {
  const base = Deno.env.get('SUPABASE_URL');
  const key = Deno.env.get('SUPABASE_SERVICE_ROLE_KEY');
  if (!base || !key) throw new Error('STORE_NOT_CONFIGURED');
  const r = await fetch(`${base}/rest/v1/rpc/${name}`, {method:'POST',headers:{apikey:key,Authorization:`Bearer ${key}`,'Content-Type':'application/json'},body:JSON.stringify(args)});
  if (!r.ok) {
    const e = await r.json().catch(() => ({}));
    const safe = ['PREVIOUS_RUN_UNRESOLVED','OUT_OF_ORDER_DATE','STATE_CHANGED','RESERVATION_REQUIRED','BUNDLE_INTEGRITY','IMMUTABLE_RESULT','REPORT_IDENTITY','STATE_IDENTITY','STATE_REQUIRED','FAILED_STATE_NOT_PUBLISHABLE'];
    throw new Error(safe.includes(e.message) ? e.message : 'STORE_OPERATION_REJECTED');
  }
  return await r.json();
}
Deno.serve(async (req: Request) => {
  try {
    const token = req.headers.get('authorization')?.match(/^Bearer ([A-Za-z0-9_.-]+)$/)?.[1];
    if (!token) return json({error:'UNAUTHORIZED'},401);
    let identity;
    try {
      const {payload} = await jwtVerify(token, keys, {issuer:'https://token.actions.githubusercontent.com',audience:AUDIENCE,algorithms:['RS256'],requiredClaims:['exp','iat','nbf','sub'],maxTokenAge:'10m',clockTolerance:5});
      identity = authorize(payload);
    } catch { return json({error:'UNAUTHORIZED'},401); }
    if (req.method !== 'POST') return json({error:'METHOD_NOT_ALLOWED'},405);
    const length = Number(req.headers.get('content-length'));
    if (!Number.isSafeInteger(length) || length<=0 || length>13000000) return json({error:'BODY_LIMIT'},413);
    const body = await req.text();
    if (new TextEncoder().encode(body).length>13000000) return json({error:'BODY_LIMIT'},413);
    const p = JSON.parse(body);
    if (!p || Array.isArray(p) || typeof p !== 'object' || !/^\d{4}-\d{2}-\d{2}$/.test(p.date ?? '')) return json({error:'INVALID_REQUEST'},400);
    const day = new Date(`${p.date}T00:00:00+08:00`);
    const now = new Date();
    const local = new Date(now.getTime()+8*3600000);
    const today = local.toISOString().slice(0,10);
    if (!Number.isFinite(day.getTime()) || new Date(day.getTime()+8*3600000).toISOString().slice(0,10)!==p.date || p.date>today || day.getTime()<now.getTime()-32*86400000) return json({error:'DATE_OUT_OF_RANGE'},400);
    const route = new URL(req.url).pathname.split('/').pop();
    let result;
    if (route==='context') {
      if (p.compare_date!==null && (!/^\d{4}-\d{2}-\d{2}$/.test(p.compare_date ?? '') || p.compare_date>=p.date)) return json({error:'INVALID_COMPARE_DATE'},400);
      result = await rpc('trade_daily_context',{p_date:p.date,p_compare_date:p.compare_date});
    } else if (route==='start') {
      if (!/^[0-9a-f]{40}$/.test(p.strategy_sha ?? '')) return json({error:'INVALID_SOURCE'},400);
      const hour = local.getUTCHours()*100+local.getUTCMinutes();
      if (p.date===today && hour<1530) return json({error:'CLOSE_NOT_READY'},409);
      result = await rpc('trade_daily_start',{p_date:p.date,p_run_id:identity.run,p_attempt:identity.attempt,p_strategy_sha:p.strategy_sha,p_workflow_sha:identity.sha,p_previous_date:p.previous_date ?? null});
    } else if (route==='finish') {
      if (!['SUCCESS','DEGRADED','FAILED'].includes(p.status) || typeof p.bundle!=='string' || typeof p.markdown!=='string' || p.markdown.length>200000 || !/^[0-9a-f]{64}$/.test(p.sha256 ?? '')) return json({error:'INVALID_RESULT'},400);
      result = await rpc('trade_daily_finish',{p_date:p.date,p_run_id:identity.run,p_status:p.status,p_report:p.report,p_markdown:p.markdown,p_risk_state:p.risk_state ?? null,p_bundle:p.bundle,p_sha256:p.sha256});
    } else if (route==='result') {
      result = await rpc('trade_daily_result',{p_date:p.date});
    } else if (route==='event') {
      if (!['HOLIDAY','BEFORE_CLOSE','PREPARATION_FAILED','DELIVERY_FAILED','VERIFIED'].includes(p.status) || JSON.stringify(p.details).length>40000) return json({error:'INVALID_EVENT'},400);
      result = await rpc('trade_daily_event',{p_date:p.date,p_run_id:identity.run,p_attempt:identity.attempt,p_status:p.status,p_details:p.details});
    } else return json({error:'NOT_FOUND'},404);
    return json(result);
  } catch (e) {
    const message = e instanceof Error && /^[A-Z_]{3,60}$/.test(e.message) ? e.message : 'PRIVATE_OPERATION_FAILED';
    return json({error:message},503);
  }
});
