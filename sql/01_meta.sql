create schema if not exists meta;

create table if not exists meta.load_runs (
    load_id      bigint generated always as identity primary key,
    report_date  date not null,
    window_from  date not null,
    window_to    date not null,
    started_at   timestamptz not null default now(),
    finished_at  timestamptz,
    status       text not null,
    rows_fetched integer,
    message      text
);

-- пересоздаётся, чтобы новый статус доехал и до уже созданной базы
alter table meta.load_runs drop constraint if exists load_runs_status_check;
alter table meta.load_runs add constraint load_runs_status_check
    check (status in ('running', 'published', 'failed_source', 'blocked', 'error'));

create table if not exists meta.check_results (
    load_id    bigint not null references meta.load_runs,
    check_code text not null,
    level      text not null check (level in ('error', 'warn')),
    status     text not null check (status in ('passed', 'failed', 'skipped')),
    violations integer,
    sample     jsonb,
    details    text,
    primary key (load_id, check_code)
);
