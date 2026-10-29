-- 002_seed_multi_provider.sql
-- Seed data for the multi-provider tables (run after 001). Re-runnable: every insert is an upsert.
-- Sources:
--   clinicians + clinic  : Tebra billing data Jan-Jul 2026
--   gender + pelvic flag : rauschpt.net bios, pronouns only (evidence: tests/multi_provider/clinician_research.json)
--   hours                : clinic opening hours from app/utils/time_utils.py get_location_hours()
--                          + DEMO split shifts (marked below) - replace with real rosters from the client.

begin;

-- ─── Clinics ─────────────────────────────────────────────────────────────────
insert into public.clinics (tebra_location_id, name, tebra_location_name, lead_location_label, active) values
    (1,  'Laguna Niguel',               'Rausch Physical Therapy, Inc',     'Laguna Niguel',               true),
    (7,  'Dana Point',                  'Rausch Dana Point',                'Dana Point',                  true),
    (29, 'Mission Viejo',               'Rausch PT - Mission Viejo',        'Mission Viejo',               true),
    (30, 'Fort Fitness - Laguna Hills', 'Rausch Physical Therapy - Fort F', 'Fort Fitness - Laguna Hills', true)
on conflict (tebra_location_id) do update set
    name = excluded.name, tebra_location_name = excluded.tebra_location_name,
    lead_location_label = excluded.lead_location_label, active = excluded.active;

-- ─── Visit types ─────────────────────────────────────────────────────────────
insert into public.visit_types (code, tebra_reason_id, duration_minutes, label) values
    ('evaluation', 96, 60, 'Evaluation'),
    ('follow_up',  95, 45, 'Follow up')
on conflict (code) do update set
    tebra_reason_id = excluded.tebra_reason_id, duration_minutes = excluded.duration_minutes, label = excluded.label;

-- ─── Clinicians ──────────────────────────────────────────────────────────────
-- gender 'unknown' = no bio / no pronoun on the website. Never guessed from a first name.
insert into public.clinicians (tebra_provider_id, full_name, degree, gender, gender_source, specialties, bookable, active, notes) values
    -- Laguna Niguel
    (3708, 'Sarah Young',          'DPT',  'female',  'rauschpt.net/team/dr-sarah-young (she/her)',    '{orthopedic,upper_extremity,sports}',   true,  true, null),
    (4138, 'Danielle Simons',      'DPT',  'female',  'rauschpt.net/team/dr-danielle-s (she/her)',     '{pelvic_health,orthopedic}',            true,  true, 'Clinical Director; pelvic health (men''s and women''s, postpartum)'),
    (4919, 'Emily Rodriguez',      null,   'unknown', 'no bio on rauschpt.net',                        '{}',                                    true,  true, null),
    (5490, 'Byron Samayoa',        'DPT',  'male',    'rauschpt.net/team/byron (he/his)',              '{orthopedic,geriatrics,sports}',        true,  true, 'Bilingual Spanish/English'),
    (5875, 'Jean Charles Plantier', null,  'unknown', 'no bio on rauschpt.net',                        '{}',                                    true,  true, null),
    (6018, 'Daniel Cerfogli',      'DPT',  'male',    'rauschpt.net/team/dr-daniel-c (his)',           '{orthopedic,sports}',                   true,  true, null),
    (4918, 'Joseph Austin McKee',  'DPT',  'male',    'rauschpt.net/team/dr-austin-mckee (he/his)',    '{orthopedic,sports,post_operative}',    true,  true, 'Goes by Austin'),
    (5776, 'Bailey Advincula',     null,   'unknown', 'no bio on rauschpt.net',                        '{}',                                    true,  true, null),
    (4917, 'Chloe Remmert',        null,   'unknown', 'no bio on rauschpt.net',                        '{}',                                    true,  true, null),
    (2628, 'Camery Cano',          null,   'unknown', 'no bio on rauschpt.net (testimonial only, no pronoun)', '{}',                            true,  true, null),
    (5665, 'Katia Correa',         null,   'unknown', 'no bio on rauschpt.net',                        '{}',                                    true,  true, null),
    (6199, 'Kevin Takao',          'MPT',  'male',    'rauschpt.net/team/kevin-t (his)',               '{orthopedic}',                          true,  true, null),
    (6200, 'Prajna Acharya',       'DPT',  'female',  'rauschpt.net/team/dr-prajna-a (she)',           '{orthopedic,sports,post_operative}',    true,  true, null),
    -- Dana Point
    (3114, 'Clay Simons',          'DPT',  'male',    'rauschpt.net/team/clay-s (him)',                '{orthopedic,manual_therapy,sports}',    true,  true, 'Assistant Clinical Director'),
    (5543, 'Andrew Lincoln',       'DPT',  'male',    'rauschpt.net/team/andrew-l (him)',              '{orthopedic,sports}',                   true,  true, null),
    (4410, 'Nick Takekoshi',       'DPT',  'male',    'rauschpt.net/team/nick-t (he/his)',             '{orthopedic,sports,concussion}',        true,  true, 'Group Director'),
    (5379, 'Ryan Tran',            'DPT',  'male',    'rauschpt.net/team/ryan-t (him/his)',            '{orthopedic,sports,shoulder_neck}',     true,  true, null),
    -- Mission Viejo / Fort Fitness
    (2172, 'Sarah MacMillan',      null,   'unknown', 'no bio on rauschpt.net',                        '{}',                                    true,  true, null),
    (5955, 'Analise Richtsmeier',  'DPT',  'female',  'rauschpt.net/team/dr-analise-r (she/her)',      '{pelvic_health,orthopedic}',            true,  true, 'Pelvic Health Specialist (PRPC, PCES)'),
    (5683, 'Joelle Shoemaker',     'PT',   'female',  'rauschpt.net/team/joelle-s (she/her)',          '{orthopedic}',                          true,  true, 'Works Mission Viejo and Fort Fitness'),
    -- Not bookable by the AI
    (1,    'Kevin M Rausch',       null,   'male',    'rauschpt.net/team/kevin-rausch (he/his)',       '{}',                                    false, true,  'Owner / CEO - few visits, never AI-booked'),
    (4387, 'Kaitlynne Forbes',     null,   'unknown', 'no bio on rauschpt.net',                        '{}',                                    false, false, 'No visits in billing data - likely inactive')
on conflict (tebra_provider_id) do update set
    full_name = excluded.full_name, degree = excluded.degree, gender = excluded.gender,
    gender_source = excluded.gender_source, specialties = excluded.specialties,
    bookable = excluded.bookable, active = excluded.active, notes = excluded.notes;

-- ─── Clinician -> clinic -> service line ────────────────────────────────────
-- physical_therapy: every bookable clinician at their clinic(s). pelvic_health: website-flagged only.
insert into public.clinician_services (tebra_provider_id, tebra_location_id, service_line) values
    (3708, 1, 'physical_therapy'), (4138, 1, 'physical_therapy'), (4919, 1, 'physical_therapy'),
    (5490, 1, 'physical_therapy'), (5875, 1, 'physical_therapy'), (6018, 1, 'physical_therapy'),
    (4918, 1, 'physical_therapy'), (5776, 1, 'physical_therapy'), (4917, 1, 'physical_therapy'),
    (2628, 1, 'physical_therapy'), (5665, 1, 'physical_therapy'), (6199, 1, 'physical_therapy'),
    (6200, 1, 'physical_therapy'),
    (3114, 7, 'physical_therapy'), (5543, 7, 'physical_therapy'), (4410, 7, 'physical_therapy'),
    (5379, 7, 'physical_therapy'),
    (2172, 29, 'physical_therapy'), (5955, 29, 'physical_therapy'), (5683, 29, 'physical_therapy'),
    (5683, 30, 'physical_therapy'),
    (4138, 1,  'pelvic_health'),
    (5955, 29, 'pelvic_health')
on conflict do nothing;

-- ─── Weekly hours = clinic opening hours ─────────────────────────────────────
-- LN Mon-Fri 7-19 + Sat 8-14 ; DP Mon-Fri 7-19 ; MV Mon-Fri 7-17 (DP/MV Saturdays not bookable).
-- Days replaced by a DEMO split shift (further down) are skipped here.
insert into public.clinician_hours (tebra_provider_id, tebra_location_id, weekday, start_time, end_time)
select p.id, p.loc, d.wd,
       case when d.wd = 5 then time '08:00' else time '07:00' end,
       case when d.wd = 5 then time '14:00' when p.loc = 29 then time '17:00' else time '19:00' end
from (values
        (3708, 1), (4138, 1), (4919, 1), (5490, 1), (5875, 1), (6018, 1), (4918, 1),
        (5776, 1), (4917, 1), (2628, 1), (5665, 1), (6199, 1), (6200, 1),
        (3114, 7), (5543, 7), (4410, 7), (5379, 7),
        (2172, 29), (5955, 29)
     ) as p(id, loc)
cross join generate_series(0, 5) as d(wd)
where (d.wd <= 4 or p.loc = 1)                 -- Saturday only at Laguna Niguel
  and not (p.id = 3708 and d.wd = 0)           -- DEMO split: Sarah Young Monday
  and not (p.id = 3114 and d.wd = 4)           -- DEMO short day: Clay Simons Friday
on conflict (tebra_provider_id, tebra_location_id, weekday, start_time) do update set end_time = excluded.end_time;

-- Joelle Shoemaker works two clinics, so her week is split between them (no overlap):
--   Mission Viejo Mon + Wed 7-17 ; Fort Fitness Thu 8-17 + Fri 8-13 (Fort Fitness hours) ; Tue = DEMO split below.
insert into public.clinician_hours (tebra_provider_id, tebra_location_id, weekday, start_time, end_time) values
    (5683, 29, 0, '07:00', '17:00'),
    (5683, 29, 2, '07:00', '17:00'),
    (5683, 30, 3, '08:00', '17:00'),
    (5683, 30, 4, '08:00', '13:00')
on conflict (tebra_provider_id, tebra_location_id, weekday, start_time) do update set end_time = excluded.end_time;

-- ─── DEMO split shifts (made up for testing - not real rosters) ──────────────
insert into public.clinician_hours (tebra_provider_id, tebra_location_id, weekday, start_time, end_time) values
    (3708, 1,  0, '07:00', '11:00'),   -- DEMO: Sarah Young Mon 7-11 ...
    (3708, 1,  0, '14:00', '19:00'),   -- ... and 14-19 (lunch/admin gap)
    (5683, 29, 1, '07:00', '12:00'),   -- DEMO: Joelle Shoemaker Tue 7-12 Mission Viejo ...
    (5683, 30, 1, '13:00', '17:00'),   -- ... and 13-17 Fort Fitness
    (3114, 7,  4, '07:00', '12:00')    -- DEMO: Clay Simons Fri 7-12 only
on conflict (tebra_provider_id, tebra_location_id, weekday, start_time) do update set end_time = excluded.end_time;

commit;
