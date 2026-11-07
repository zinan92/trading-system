CREATE TABLE judgments (
  id integer primary key autoincrement,
  created_at text not null,
  asset text not null,
  direction text not null check (direction in ('long','short','flat')),
  confidence integer not null check (confidence between 1 and 5),
  reason text not null default '',
  cited text not null default '[]',
  price_at real,
  plan text,
  action text not null check (action in ('recorded','approved')),
  resolved_at text,
  price_after real,
  move_pct real,
  outcome text
, author text not null default 'park', evidence text not null default '{}', advice text);
CREATE TABLE notes (
  id integer primary key autoincrement,
  created_at text not null,
  asset text not null,
  body text not null
);
CREATE TABLE executions (
  id integer primary key autoincrement,
  created_at text not null,
  judgment_id integer,
  asset text not null,
  stage text not null,
  preview_digest text,
  detail text not null default '{}'
);
CREATE TABLE assets (
  key text primary key,
  label text not null,
  kind text not null check (kind in ('hl_testnet','xau_paper','watch')),
  venue_profile_id text not null,
  instrument_id text not null,
  coin text not null,
  news_queries text not null default '[]',
  primary_words text not null default '[]',
  kline_key text,
  position integer not null default 100,
  created_at text not null
);
CREATE TABLE meta (
  key text primary key,
  value text not null
);
