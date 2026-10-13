-- Rausch PT clinic FAQ (source: client VoiceFAQ, Sep 2026). Run once in Supabase SQL editor.
create table if not exists public.clinic_faq (
  id bigserial primary key,
  practice text not null default 'rausch_pt',
  category text not null,
  question text not null,
  answer text not null,
  follow_up boolean not null default false,
  keywords text,
  active boolean not null default true,
  updated_at timestamptz not null default now()
);
alter table public.clinic_faq enable row level security;
drop policy if exists "clinic_faq read" on public.clinic_faq;
create policy "clinic_faq read" on public.clinic_faq for select to anon, authenticated using (active);
delete from public.clinic_faq where practice = 'rausch_pt';
insert into public.clinic_faq (category, question, answer, follow_up) values
  ('Always hand off without answering', 'Always hand off without answering', 'Medical emergency: tell the caller to hang up and dial 911.
- Symptoms, diagnosis, or whether an exercise or treatment is safe for them.
- Billing disputes, balances, or questions about a specific bill.
- Workers'' comp, auto accident, or personal injury cases.
- Complaints, or an upset caller who asks for a person.', false),
  ('Core facts', 'Core facts', 'Clinic name: Rausch PT & Wellness (also known as Rausch Physical Therapy & Sports Performance).
- Main phone: (949) 276-5401, one number for all three locations.
- Phone hours: Monday–Friday, 7 AM–7 PM.
- Locations: Laguna Niguel, Dana Point, Mission Viejo.
- Website: rauschpt.net
- Referral needed: No, for physical therapy or wellness services (California Direct Access).
- Online booking: available 24/7 for PT and wellness services at rauschpt.net.', false),
  ('General clinic information', 'What kind of clinic is this?', 'We''re Rausch PT & Wellness, a physical therapy and wellness clinic with three locations in South Orange County. We offer physical therapy, pelvic health therapy, chiropractic care, therapeutic massage, and assisted stretching, all under one roof. Our goal is to give you personalized care that gets you back to doing what you love.', false),
  ('General clinic information', 'What makes you different from other PT clinics?', 'You see the same physical therapy care team every visit, so your providers really get to know your body and your goals. Because massage, stretching, and chiropractic are in-house, the team works together on a plan that treats your whole body, not just the spot that hurts.', false),
  ('General clinic information', 'Who do you usually treat?', 'Everyone from weekend warriors to people recovering from surgery: runners, surfers, golfers, pickleball and tennis players, dancers, older adults working on balance, and anyone with aches, pains, or stiffness in daily life.', false),
  ('General clinic information', 'Do I need a doctor''s referral?', 'No. In California you can see a physical therapist directly without a referral. The same goes for massage, stretching, and chiropractic. If your condition needs a doctor''s evaluation, we''ll let you know and help connect you with a trusted local physician.', false),
  ('General clinic information', 'Is this the same place as Rausch Physical Therapy and Sports Performance?', 'Yes. Rausch Physical Therapy & Sports Performance is our longtime name; today we go by Rausch PT & Wellness.', false),
  ('General clinic information', 'Do you have a website?', 'Yes, rauschpt.net. You can see the team, pricing, and accepted insurance there, and book appointments online anytime.', false),
  ('Locations and directions', 'Where are you located?', 'Three locations in South Orange County: Laguna Niguel, Dana Point, and Mission Viejo. All three offer physical therapy.', false),
  ('Locations and directions', 'Laguna Niguel address', '30100 Town Center Drive, Suite Y/Z, Laguna Niguel, CA 92677. Open every Saturday morning.
Email: info@rauschpt.net. Fax: (949) 276-5403.', false),
  ('Locations and directions', 'Dana Point address', '24582 Del Prado Avenue, Suite C, Dana Point, CA 92629.
Email: infoDP@rauschpt.net. Fax: (949) 443-0721.', false),
  ('Locations and directions', 'Mission Viejo address', '24000 Alicia Parkway, Suite 27, Mission Viejo, CA 92691.
Email: infoMV@rauschpt.net. Fax: (949) 245-7544.', false),
  ('Locations and directions', 'Which location is closest to me?', 'Ask which city or neighborhood the caller is coming from and help them pick. All three offer physical therapy, so choose whichever is easiest to get to.', false),
  ('Locations and directions', 'Is there parking?', 'The front desk will give the best parking details for the location you''re visiting.', true),
  ('Locations and directions', 'Is the clinic wheelchair accessible?', 'The front desk will walk you through access at your location so you get accurate details.', true),
  ('Locations and directions', 'Can you send me directions?', 'Each location has a "Get Directions" link at rauschpt.net under Locations, or search "Rausch Physical Therapy" plus the city name in your maps app.', false),
  ('Operating hours', 'Hours by location', 'Laguna Niguel: Monday–Friday 7 AM–7 PM; every Saturday 8 AM–2 PM.
- Dana Point: Monday–Friday 7 AM–7 PM; alternate Saturdays 8 AM–2 PM.
- Mission Viejo: Monday–Friday 7 AM–5 PM; alternate Saturdays 8 AM–2 PM.
- Sunday: closed at all locations.
- Phones are answered Monday–Friday, 7 AM–7 PM, for all three locations.', false),
  ('Operating hours', 'Are you open on Saturdays?', 'Yes. Laguna Niguel is open every Saturday, 8 AM–2 PM. Dana Point and Mission Viejo are open on alternating Saturdays, same hours, so check availability when booking.', false),
  ('Operating hours', 'Is Dana Point or Mission Viejo open this Saturday?', 'Those two locations are open every other Saturday, 8 AM–2 PM. The front desk can confirm whether this Saturday is one of them. For a guaranteed Saturday, Laguna Niguel is open every week.', true),
  ('Operating hours', 'Are you open on Sundays?', 'Closed on Sundays at all locations. You can still book online anytime at rauschpt.net, or call back Monday starting at 7 AM.', false),
  ('Operating hours', 'How early can I come in before work?', 'All three clinics open at 7 AM on weekdays. Laguna Niguel and Dana Point also have evening appointments until 7 PM.', false),
  ('Operating hours', 'Are you open on holidays?', 'Hours can change around holidays. The front desk will confirm the schedule for the date you have in mind.', true),
  ('Operating hours', 'How do I reach someone after hours?', 'Phones are answered Monday–Friday, 7 AM–7 PM. Outside those hours, book or request an appointment online at rauschpt.net, or email the location you visit and the team will get back to you. For a medical emergency, hang up and call 911.', false),
  ('Our team', 'Physical therapists — Laguna Niguel', 'Dr. Danielle S. — Clinical Director, Pelvic Health & Orthopedic Specialist — pelvic health, orthopedics, active adults.
- Dr. Emily R. — Physical Therapist, Pelvic Health Specialist — dancers; knee, ankle, and hip injuries; pelvic health.
- Dr. Daniel C. — Physical Therapist — sports, active adults and athletes.
- Christina K. — Physical Therapist — low back and lower-body conditions.
- Dr. Austin M. — Physical Therapist — hip, ankle, post-surgery rehab.
- Dr. Peyton L. — Physical Therapist — balance, dizziness (vestibular), jaw (TMJ).
- Dr. Chloe R. — Physical Therapist, Pelvic Health Specialist — women''s pelvic health, pregnancy and postpartum, dancers.
- Dr. Sarah Y. — Physical Therapist — shoulder, arm, and hand; sports injuries.
- Kevin T. — Chief Operations Officer, Physical Therapist — 25+ years of general orthopedic care.
- Dr. Bailey A. — Physical Therapist — neurological and balance, surf injuries, neck.
- Dr. Byron S. — Physical Therapist (bilingual) — older adults, soccer players, cyclists.
- Dr. Prajna A. — Physical Therapist — orthopedic, post-surgery, sports (11+ years).
- Ryan H. — Physical Therapist Assistant — orthopedic, neurological, workers'' comp.
- Abby A. — Physical Therapist Assistant, Pelvic Health Specialist — pelvic floor, Pilates.', false),
  ('Our team', 'Physical therapists — Dana Point', 'Dr. Nick T. — Group Director, Physical Therapist — surfers, runners, concussion rehab.
- Dr. Clay S. — Assistant Clinical Director, Physical Therapist — hands-on (manual) therapy, surfers, active adults.
- Dr. Ryan T. — Physical Therapist (also StretchFix) — neck and shoulder, surfers, children with disabilities.
- Dr. Andrew L. — Physical Therapist — volleyball athletes, surfers.
- Dr. Jordan R. — Physical Therapist — orthopedic, neurological, sports, pediatric.
- Kathryn T. — Physical Therapist Assistant, Pelvic Health Specialist — orthopedic, Pilates, dancers (also sees patients in Mission Viejo).', false),
  ('Our team', 'Physical therapists — Mission Viejo', 'Dr. Sarah T. — Clinical Director, Physical Therapist — orthopedic and sports, active adults.
- Dr. Analise R. — Physical Therapist, Pelvic Health Specialist — pelvic floor, runners and active adults.
- Joelle S. — Physical Therapist — orthopedic, 30+ years of experience.
- Kathryn T. — Physical Therapist Assistant, Pelvic Health Specialist — orthopedic, Pilates, dancers.', false),
  ('Our team', 'Wellness providers', 'Chiropractors (Laguna Niguel and Dana Point): Dr. Peter S. and Dr. Bill Cowdrey (Certified Chiropractic Sports Physician).
- Massage therapists: Rolan F. (Director of Health & Wellness), Roni M., Melody D., My T., Natalie C., Rook, Marc V., Irina E., Meagen J., Faviola, Alina, Jeff.
- StretchFix specialists ("Stretch Fixologists"): Reagan, Jorge P., Brisa, Alexis M., Ethan, Anthony, Rachel V., Jeff, Dr. Ryan T.', false),
  ('Our team', 'Leadership', 'Kevin R. (President, Owner, and Founder), Kevin T. (COO), Jessica Rojas (Regional Manager).', false),
  ('Our team', 'Who will be treating me?', 'Your care is led by a licensed physical therapist, and you see the same physical therapy care team every visit.', false),
  ('Our team', 'Can I request a specific therapist?', 'Absolutely. Tell us who you''d like to see and we''ll check which location they work at. If you''re not sure, describe what''s going on and we can suggest someone whose focus fits.', false),
  ('Our team', 'Pelvic floor / pelvic health specialists', 'Several pelvic health specialists; pelvic health therapy is available to all genders. Laguna Niguel: Dr. Danielle, Dr. Emily, Dr. Chloe, and Abby. Mission Viejo: Dr. Analise. Kathryn sees patients in both Dana Point and Mission Viejo.', false),
  ('Our team', 'Therapist for surfers, runners, dancers, golfers, athletes', 'Surfers: Dr. Nick, Dr. Clay, Dr. Andrew, and Dr. Ryan in Dana Point, and Dr. Bailey in Laguna Niguel. Dancers: Dr. Emily and Dr. Chloe in Laguna Niguel. Happy to help match any other sport.', false),
  ('Our team', 'Therapist for dizziness or balance problems', 'Dr. Peyton and Dr. Bailey in Laguna Niguel focus on balance and dizziness. Dr. Peyton also treats jaw (TMJ) problems.', false),
  ('Our team', 'Does anyone speak Spanish or another language?', 'Some team members are bilingual. The front desk will match you with the right provider.', true),
  ('Our team', 'I used to see a provider who is not listed. Can I book with them?', 'The front desk can check that provider''s current schedule or help find the best fit on the team.', true),
  ('Services offered', 'Services and session lengths', 'Physical therapy — hands-on treatment and targeted exercise to find and fix the root cause of pain — about 60 minutes.
- Premium physical therapy — extended one-on-one time with your physical therapist — 45 or 60 minutes.
- Pelvic health therapy — pelvic floor treatment for all genders, including pregnancy and postpartum — 60 minutes.
- Rx Chiro — chiropractic care starting with a full neurological evaluation — 10 to 60 minutes.
- Rx Massage — therapeutic massage for recovery and injury prevention — 50 or 80 minutes.
- StretchFix — assisted stretching designed by Doctors of Physical Therapy — 25 or 50 minutes.
- Rx Wellness Membership — monthly plans for massage and StretchFix, with member discounts.', false),
  ('Services offered', 'What do you treat?', 'Most aches, pains, sprains, and movement problems: back and neck pain, sports injuries, recovery after surgery, joint pain, balance and dizziness, jaw pain, concussion recovery, and pelvic floor concerns.', false),
  ('Services offered', 'Do you do rehab after surgery?', 'Yes, before and after surgery, such as knee, hip, and shoulder procedures. The therapist builds a plan around your surgeon''s guidelines and your goals.', false),
  ('Services offered', 'What happens in a physical therapy session?', 'Your therapist looks at how your whole body moves to find the root cause of your symptoms. Sessions combine hands-on treatment with guided exercises, and the plan is adjusted as you improve.', false),
  ('Services offered', 'What is Premium physical therapy?', '45 or 60 minutes of dedicated one-on-one time with your physical therapist. Great if you want extra hands-on care, are recovering from surgery, or want to get back to your sport faster. You can use insurance to cover part of the cost or pay out of pocket.', false),
  ('Services offered', 'What is pelvic floor therapy?', 'Helps with issues like pelvic pain, core weakness, and concerns during pregnancy or after giving birth, among many others. Available to all genders; sessions are private, one-on-one, and about 60 minutes.', false),
  ('Services offered', 'Do you have a chiropractor?', 'Yes. Rx Chiro includes adjustments, hands-on soft tissue work, and corrective exercises. Every new chiropractic client starts with a 45-minute full-body evaluation before booking other chiro visits.', false),
  ('Services offered', 'Do you offer massage?', 'Yes. Rx Massage offers 50- and 80-minute therapeutic sessions personalized with targeted techniques. Great for recovery, injury prevention, or helping your body feel better.', false),
  ('Services offered', 'What is StretchFix?', 'Assisted stretching program designed by Doctors of Physical Therapy. A trained specialist stretches you through personalized routines to improve flexibility and mobility. Sessions are 25 or 50 minutes.', false),
  ('Services offered', 'What is the Rx Wellness Membership?', 'A monthly plan for massage or StretchFix. Ground: $87 a month for one 50-minute session. Evolve: $163 a month for two. Elevate: $347 a month for three, plus two 30-minute add-ons and 10 laser sessions. All plans include discounts on additional wellness services.', false),
  ('Services offered', 'How do I cancel my membership?', 'Membership cancellations need 30 days'' notice, using the cancellation form on the Rx Wellness Membership page. Once filled out, email it to info@rauschpt.net.', true),
  ('Services offered', 'Do you treat kids?', 'Some therapists work with children. The front desk will confirm the right provider and location for your child.', true),
  ('Appointments and booking', 'How do I make an appointment?', 'The agent can help schedule right now. You can also book physical therapy or wellness services online anytime at rauschpt.net.', false),
  ('Appointments and booking', 'I''m a new patient. How do I get started?', 'For physical therapy, you start with an initial evaluation, where your therapist learns about your injury and goals and builds your treatment plan. No referral needed.', false),
  ('Appointments and booking', 'I''m a returning patient. How do I book my next visit?', 'Book with the agent now, online at rauschpt.net, or with the front desk at the end of your appointment.', false),
  ('Appointments and booking', 'Can I get in today?', 'We''ll do our best to get you in as soon as possible. You can also see live availability online at rauschpt.net.', true),
  ('Appointments and booking', 'How do I cancel or reschedule? (cancellation policy)', 'Physical therapy: let us know by 2 PM the business day before your visit (for a Monday appointment, by 2 PM Friday). Massage, StretchFix, and chiropractic: at least 24 hours'' notice.', false),
  ('Appointments and booking', 'Is there a fee if I cancel late or miss my appointment?', 'Yes. Physical therapy late cancellation or no-show: $55 for a standard session, $110 for a premium session. Massage, StretchFix, and chiropractic: changes within 24 hours are charged the full appointment fee.', false),
  ('Appointments and booking', 'How long is each appointment?', 'Physical therapy about 60 minutes; Premium PT 45 or 60 minutes; massage 50 or 80 minutes; StretchFix 25 or 50 minutes; chiropractic 10 to 60 minutes depending on the type.', false),
  ('Appointments and booking', 'How often will I need to come in?', 'Depends on your personal treatment plan. After your first visit, your therapist recommends how often to come in and gives an estimated timeline, which can change as you improve.', false),
  ('Appointments and booking', 'How long will I need physical therapy?', 'Depends on your condition, goals, and how your body responds. Your therapist gives an estimated timeline at your first visit and adjusts it as you progress.', false),
  ('Appointments and booking', 'Can I book a massage or StretchFix while doing PT?', 'Yes, many patients do. Wellness providers and physical therapists work together, so these services can support your recovery. Book online anytime at rauschpt.net.', false),
  ('Appointments and booking', 'Do I need a chiropractic evaluation first?', 'Yes. New chiropractic clients start with a 45-minute full-body evaluation, including a neurological exam, diagnosis, and a first treatment. After that you can book follow-up, advanced, or quick maintenance visits.', false),
  ('Insurance and payment', 'Accepted insurance plans (all three locations)', 'PPO (no referral needed): Blue Cross, Blue Shield, UnitedHealthcare, Cigna, Aetna, Medicare, AARP, UMR, Tricare, Humana, MultiPlan, PHCS.
- HMO (medical group must be MemorialCare): Blue Cross, Blue Shield, UnitedHealthcare, Cigna, Aetna, Medicare, AARP, UMR, Tricare, SCAN, Health Net.
- Workers'' comp carriers: One Call, MedRisk, Sedgwick, CorVel, Homelink, CiQ.', false),
  ('Insurance and payment', 'Do you take my insurance?', 'We accept most major plans, including Blue Cross, Blue Shield, UnitedHealthcare, Cigna, Aetna, Medicare, AARP, UMR, Tricare, Humana, MultiPlan, and PHCS. If a plan is not on the list, the front desk is happy to check.', false),
  ('Insurance and payment', 'I have an HMO. Can I come to you?', 'Yes, with several HMO plans, as long as your medical group is MemorialCare. The medical group is usually printed on your insurance card, and the front desk can help confirm.', false),
  ('Insurance and payment', 'Cash-pay prices (self-pay rates)', 'PT initial evaluation or premium session, 60 min: $197.
- Premium PT session, 45 min: $157.
- PT follow-up session, 30 min: $107.
- Pelvic health session, 60 min: $249.
- Rx Massage: 50 min $107; 80 min $137.
- StretchFix: 25 min $63; 50 min $107.
- StretchFix 3-pack: 3 × 25 min $105; 3 × 50 min $197.
- Chiro full-body evaluation, 45 min: $153.
- Chiro advanced treatment, 60 min: $197.
- Chiro follow-up, 30 min: $97.
- Chiro maintenance visit, 10 min: $43.', false),
  ('Insurance and payment', 'Will my insurance cover all of it? What will I owe?', 'Most plans cover physical therapy, though you may have a co-pay at each visit. The front desk can review your benefits before your first appointment, or you can call the number on the back of your insurance card.', true),
  ('Insurance and payment', 'Do you take Medi-Cal or Kaiser?', 'The front desk will check your specific plan and go over your options.', true),
  ('Insurance and payment', 'What if I don''t have insurance?', 'No problem. Straightforward cash-pay rates, for example a physical therapy evaluation is $197 and a 30-minute follow-up is $107.', false),
  ('Insurance and payment', 'Can I use insurance for massage or StretchFix?', 'Depends on your plan. Some plans may help cover these; if not, pay out of pocket at standard rates or save with a package or membership.', false),
  ('Insurance and payment', 'Does insurance cover chiropractic?', 'Chiropractic visits are cash-pay. At the end of each visit you get a detailed receipt called a superbill, which you can send to your insurance for possible reimbursement depending on your plan.', false),
  ('Insurance and payment', 'Workers'' comp or car accident case', 'We work with several workers'' comp carriers. These cases need a few extra details, so the front desk will set everything up correctly.', true),
  ('Insurance and payment', 'Question about my bill', 'The team will look at your account and help.', true),
  ('What to expect', 'What should I bring to my first visit?', 'Photo ID, insurance card, and any referral or authorization paperwork your insurance requires. Also helpful: related medical records (X-rays, MRI reports, surgery notes) and a list of current medications.', false),
  ('What to expect', 'What should I wear?', 'Comfortable clothes that let you move easily and let your therapist reach the area being treated, for example shorts for a knee or a tank top for a shoulder. Athletic shoes are a good idea.', false),
  ('What to expect', 'What happens at the first appointment?', 'Your therapist talks with you about your injury and goals, then does a thorough exam (which can include how you walk and move), explains what they found and their plan. You usually start treatment and exercises the same day.', false),
  ('What to expect', 'How long is the first visit?', 'About 60 minutes: evaluation, explaining your plan, and your first treatment.', false),
  ('What to expect', 'Will I get exercises to do at home?', 'Yes. You get a personalized home exercise program through an app called Limber Health.', false),
  ('What to expect', 'Should I arrive early?', 'Arriving a few minutes early on your first visit is a good idea so you can check in without feeling rushed.', false),
  ('What to expect', 'Anything else I should prepare?', 'Jot down questions or goals (like getting back to a sport or sleeping without pain). A water bottle and a notebook or phone for exercise notes are handy.', false),
  ('What to expect', 'Will it hurt?', 'Our goal is to help you feel better; your therapist works within your comfort level and checks in as you go. For your specific situation, the team will follow up.', true),
  ('What to expect', 'I''m in a lot of pain right now. What should I do?', 'If this is a medical emergency, hang up and call 911. Otherwise the team will follow up to find you the soonest appointment.', true);
