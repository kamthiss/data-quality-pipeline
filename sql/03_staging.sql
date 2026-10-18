create schema if not exists staging;

create table if not exists staging.orders_candidate (
    load_id        bigint not null,
    request_date   date not null,
    order_id       text,
    created_at     text,
    updated_at     text,
    customer_id    text,
    city           text,
    status         text,
    amount_rub     text,
    items_count    text,
    payment_method text
);

create table if not exists staging.orders (
    order_id       text primary key,
    created_at     timestamptz not null,
    updated_at     timestamptz not null,
    customer_id    text not null,
    city           text,
    status         text not null,
    amount_rub     numeric(12, 2) not null,
    items_count    integer not null,
    payment_method text not null,
    report_date    date not null
);

create index if not exists orders_report_date on staging.orders (report_date);
