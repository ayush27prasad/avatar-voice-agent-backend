create extension if not exists "pgcrypto";

-- Users table
create table if not exists users (
    id uuid primary key default gen_random_uuid(),
    contact_number text unique not null,
    name text,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create index if not exists users_contact_number_idx
    on users (contact_number);

-- Appointments table
create table if not exists appointments (
    id uuid primary key default gen_random_uuid(),
    contact_number text not null,
    name text,
    slot_date date not null,
    slot_time text not null,
    status text not null default 'booked',
    notes text,
    created_at timestamptz not null default now()
);

create index if not exists appointments_contact_number_idx
    on appointments (contact_number);

create index if not exists appointments_slot_idx
    on appointments (slot_date, slot_time);

create table if not exists conversation_summaries (
    id uuid primary key default gen_random_uuid(),
    contact_number text not null,
    summary text not null,
    preferences jsonb,
    booked_slots jsonb,
    created_at timestamptz not null default now()
);

create index if not exists conversation_summaries_contact_number_idx
    on conversation_summaries (contact_number);
