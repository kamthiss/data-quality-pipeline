create schema if not exists raw;

create table if not exists raw.api_pages (
    load_id      bigint not null references meta.load_runs,
    request_date date not null,
    page         integer not null,
    fetched_at   timestamptz not null default now(),
    payload      jsonb not null,
    primary key (load_id, request_date, page)
);

create table if not exists raw.api_summaries (
    load_id      bigint not null references meta.load_runs,
    request_date date not null,
    fetched_at   timestamptz not null default now(),
    payload      jsonb not null,
    primary key (load_id, request_date)
);
