create schema if not exists meta;

create table if not exists meta.load_runs (
    load_id      bigint generated always as identity primary key,
    report_date  date not null,
    window_from  date not null,
    window_to    date not null,
    started_at   timestamptz not null default now(),
    finished_at  timestamptz,
    status       text not null check (status in ('running', 'published', 'failed_source', 'blocked')),
    rows_fetched integer,
    message      text
);
