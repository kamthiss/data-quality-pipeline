create schema if not exists mart;

create table if not exists mart.daily_revenue (
    report_date    date not null,
    city           text not null,
    orders_created integer not null,
    orders_paid    integer not null,
    revenue_rub    numeric(14, 2) not null,
    cancel_rate    numeric(5, 4) not null,
    primary key (report_date, city)
);
