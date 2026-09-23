begin;
create schema trade_daily;
revoke all on schema trade_daily from public, anon, authenticated;
grant usage on schema trade_daily to service_role;
create table trade_daily.runs (
 target_date date primary key,
 run_id text not null check (run_id ~ '^[0-9]+$'),
 run_attempt integer not null check (run_attempt > 0),
 strategy_sha text not null check (strategy_sha ~ '^[0-9a-f]{40}$'),
 workflow_sha text not null check (workflow_sha ~ '^[0-9a-f]{40}$'),
 previous_state_date date,
 status text not null check (status in ('RUNNING','SUCCESS','DEGRADED','FAILED')),
 started_at timestamptz not null default now(),
 finished_at timestamptz,
 report jsonb,
 report_markdown text,
 risk_state jsonb,
 bundle bytea,
 bundle_sha256 text,
 bundle_bytes integer
);
create table trade_daily.events (
 id bigint generated always as identity primary key,
 run_id text not null,
 run_attempt integer not null,
 target_date date not null,
 status text not null,
 details jsonb not null,
 created_at timestamptz not null default now(),
 unique (run_id, run_attempt, target_date, status)
);
alter table trade_daily.runs enable row level security;
alter table trade_daily.events enable row level security;
revoke all on all tables in schema trade_daily from public, anon, authenticated;
revoke all on all sequences in schema trade_daily from public, anon, authenticated;
grant select, insert, update on trade_daily.runs to service_role;
grant select, insert on trade_daily.events to service_role;
grant usage on all sequences in schema trade_daily to service_role;

create function public.trade_daily_context(p_date date, p_compare_date date)
returns jsonb language sql security invoker set search_path=pg_catalog,trade_daily as $$
 select jsonb_build_object(
  'existing', (select jsonb_build_object('status',status,'run_id',run_id,'strategy_sha',strategy_sha) from trade_daily.runs where target_date=p_date),
  'previous', (select jsonb_build_object('date',target_date,'strategy_sha',strategy_sha,'risk_state',risk_state,'bundle',encode(bundle,'base64'),'bundle_sha256',bundle_sha256,'profile',report->'profile') from trade_daily.runs where target_date<p_date and status in ('SUCCESS','DEGRADED') and risk_state is not null order by target_date desc limit 1),
  'comparison', (select report from trade_daily.runs where target_date=p_compare_date and status in ('SUCCESS','DEGRADED'))
 );
$$;

create function public.trade_daily_start(p_date date,p_run_id text,p_attempt integer,p_strategy_sha text,p_workflow_sha text,p_previous_date date)
returns jsonb language plpgsql security invoker set search_path=pg_catalog,trade_daily as $$
declare prior date; existing trade_daily.runs%rowtype;
begin
 perform pg_advisory_xact_lock(7317171701);
 select * into existing from trade_daily.runs where target_date=p_date;
 if found then return jsonb_build_object('started',false,'status',existing.status,'run_id',existing.run_id); end if;
 if exists(select 1 from trade_daily.runs where status in ('RUNNING','FAILED')) then raise exception 'PREVIOUS_RUN_UNRESOLVED'; end if;
 if exists(select 1 from trade_daily.runs where target_date>p_date) then raise exception 'OUT_OF_ORDER_DATE'; end if;
 select max(target_date) into prior from trade_daily.runs where target_date<p_date and status in ('SUCCESS','DEGRADED') and risk_state is not null;
 if prior is distinct from p_previous_date then raise exception 'STATE_CHANGED'; end if;
 insert into trade_daily.runs(target_date,run_id,run_attempt,strategy_sha,workflow_sha,previous_state_date,status)
 values(p_date,p_run_id,p_attempt,p_strategy_sha,p_workflow_sha,p_previous_date,'RUNNING');
 return jsonb_build_object('started',true);
end; $$;

create function public.trade_daily_finish(p_date date,p_run_id text,p_status text,p_report jsonb,p_markdown text,p_risk_state jsonb,p_bundle text,p_sha256 text)
returns jsonb language plpgsql security invoker set search_path=pg_catalog,trade_daily as $$
declare r trade_daily.runs%rowtype; b bytea; h text;
begin
 select * into r from trade_daily.runs where target_date=p_date for update;
 if not found or r.run_id<>p_run_id then raise exception 'RESERVATION_REQUIRED'; end if;
 if p_status not in ('SUCCESS','DEGRADED','FAILED') then raise exception 'INVALID_STATUS'; end if;
 b:=decode(p_bundle,'base64'); h:=encode(sha256(b),'hex');
 if octet_length(b)>8388608 or h<>p_sha256 then raise exception 'BUNDLE_INTEGRITY'; end if;
 if r.status<>'RUNNING' then
  if r.bundle_sha256=h and r.status=p_status and r.report=p_report and r.report_markdown=p_markdown and r.risk_state is not distinct from p_risk_state then return jsonb_build_object('saved',true,'sha256',h,'bytes',octet_length(b)); end if;
  raise exception 'IMMUTABLE_RESULT';
 end if;
 if p_report is null or p_report->>'target_date' is distinct from p_date::text or p_report->>'strategy_sha' is distinct from r.strategy_sha or p_report->>'actions_run_id' is distinct from p_run_id or p_report->>'status' is distinct from p_status then raise exception 'REPORT_IDENTITY'; end if;
 if p_status='SUCCESS' and p_risk_state is null then raise exception 'STATE_REQUIRED'; end if;
 if p_risk_state is not null and p_risk_state->>'scan_date' is distinct from p_date::text then raise exception 'STATE_IDENTITY'; end if;
 if p_status='FAILED' and p_risk_state is not null then raise exception 'FAILED_STATE_NOT_PUBLISHABLE'; end if;
 update trade_daily.runs set status=p_status,finished_at=now(),report=p_report,report_markdown=p_markdown,risk_state=p_risk_state,bundle=b,bundle_sha256=h,bundle_bytes=octet_length(b) where target_date=p_date;
 return jsonb_build_object('saved',true,'sha256',h,'bytes',octet_length(b));
end; $$;

create function public.trade_daily_result(p_date date)
returns jsonb language sql security invoker set search_path=pg_catalog,trade_daily as $$
 select jsonb_build_object('status',status,'run_id',run_id,'report',report,'bundle',encode(bundle,'base64'),'sha256',bundle_sha256,'bytes',bundle_bytes) from trade_daily.runs where target_date=p_date;
$$;
create function public.trade_daily_event(p_date date,p_run_id text,p_attempt integer,p_status text,p_details jsonb)
returns jsonb language plpgsql security invoker set search_path=pg_catalog,trade_daily as $$
begin
 insert into trade_daily.events(target_date,run_id,run_attempt,status,details) values(p_date,p_run_id,p_attempt,p_status,p_details) on conflict do nothing;
 return jsonb_build_object('saved',true);
end; $$;
revoke all on function public.trade_daily_context(date,date),public.trade_daily_start(date,text,integer,text,text,date),public.trade_daily_finish(date,text,text,jsonb,text,jsonb,text,text),public.trade_daily_result(date),public.trade_daily_event(date,text,integer,text,jsonb) from public,anon,authenticated;
grant execute on function public.trade_daily_context(date,date),public.trade_daily_start(date,text,integer,text,text,date),public.trade_daily_finish(date,text,text,jsonb,text,jsonb,text,text),public.trade_daily_result(date),public.trade_daily_event(date,text,integer,text,jsonb) to service_role;
commit;
