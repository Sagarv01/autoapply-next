# SeekAutoApply SaaS Platform Design Spec

**Date:** 2026-04-11
**Domains:** seekautoapply.com, seekautoapply.com.au, autoapply.com.au
**Status:** Approved for implementation

## Product Overview

SeekAutoApply is a cloud-hosted SaaS platform that automates job applications on Seek.com.au. Users configure their profile, upload a resume, set search keywords, and the bot scrapes, scores, tailors, and applies to matching jobs autonomously. Two modes: Starter (static resume) and Pro (AI-tailored resume + cover letter per job).

**Target user:** Individual job seekers in Australia. Direct-to-consumer.
**Board support at launch:** Seek.com.au only (Quick Apply). Expansion to LinkedIn/Indeed planned for later.
**Execution model:** Cloud-hosted. Users never touch code, Playwright, or API keys. They configure and watch.

## Architecture

### Approach: Monolith API + Separate Workers

- **Frontend:** Next.js App Router on Vercel (free tier)
- **API:** Single FastAPI service on AWS ECS Fargate (1 vCPU, 2GB RAM, 24/7)
- **Bot workers:** Shared EC2 browser pool (t3.xlarge, 4 vCPU, 16GB RAM). Multiple users share a single instance via Playwright BrowserContexts (~30-50MB per user, ~200 users per instance). Auto-scaling group adds instances when RAM exceeds 80%.
- **Database:** PostgreSQL on RDS (db.t3.micro, single AZ, 20GB). Automated backups: 7-day retention, daily snapshots, point-in-time recovery enabled.
- **File storage:** S3 for resumes, cover letters, generated PDFs
- **Secrets:** AWS Secrets Manager for encryption keys
- **Auth:** Google OAuth with `gmail.readonly` scope (sole login method). Provides Gmail access for Seek sign-in codes and future application response tracking.
- **Billing:** Stripe AU (1.75% + $0.30 domestic, 2.9% + $0.30 international). ABN registered, GST-registered. Quarterly BAS filing required.
- **Budget:** $200 AWS credits (~3 months of infrastructure)

### System Diagram

```
                 +------------------+
                 |   Next.js App    |
                 |   (Vercel)       |
                 |   Dashboard UI   |
                 +--------+---------+
                          |
                      HTTPS API
                          |
                 +--------+---------+
                 |   FastAPI        |
                 |   (ECS Fargate)  |
                 |                  |
                 |  - Google OAuth  |
                 |  - Gmail API     |
                 |  - User CRUD    |
                 |  - Bot control  |
                 |  - Stripe       |
                 |  - WebSocket    |
                 +---+--------+----+
                     |        |
           +---------+        +---------+
           |                            |
  +--------+--------+         +--------+--------+
  |   PostgreSQL     |         |   Bot Workers   |
  |   (RDS)          |         |   (EC2 Pool)    |
  |                  |         |                  |
  |  - users         |         |  - Playwright    |
  |  - applications  |         |  - Chromium      |
  |  - searches      |         |  - BrowserContext|
  |  - billing       |         |    per user      |
  +--------+---------+         |  - ~200 users/   |
           |                   |    instance      |
           |                   +--------+---------+
  +--------+---------+                  |
  |   S3 Bucket      |<-----------------+
  |                  |   stores generated
  |  - resumes       |   PDFs + reports
  |  - cover letters |
  |  - generated PDFs|
  +-----------------+
```

## Pricing

### Free Trial
- 5 Pro-quality applications (AI-tailored resume + cover letter)
- No credit card required
- Full Pro dashboard experience (all animations, AI typing, radar, particles)
- Loss leader: costs ~$0.39 per trial user, but users experience the premium product
- After 5 apps: dashboard grays out with prominent upgrade CTA ("look what you're losing" effect)

### Starter Plan (static resume + cover letter)
| Billing | Price | Monthly Equivalent |
|---------|-------|--------------------|
| Weekly | $19.99 | $79.96/mo |
| Monthly | $49.99 | $49.99/mo (save 37%) |

### Pro Plan (AI-tailored per job)
| Billing | Price | Monthly Equivalent |
|---------|-------|--------------------|
| Weekly | $159.99 | $639.96/mo |
| Monthly | $499.99 | $499.99/mo (save 22%) |

### Margins (at typical usage ~25 apps/day, no hard limit)

| Tier | Revenue/Week | Cost/Week | Margin | Margin at 5x Cost |
|------|-------------|-----------|--------|-------------------|
| Starter Weekly | $19.99 | $0.35 | 98.2% | 85.0% |
| Starter Monthly | $12.50/wk | $0.38 | 97.0% | 85.0% |
| Pro Weekly | $159.99 | $13.48 | 91.6% | 55.8% |
| Pro Monthly | $125.00/wk | $14.44 | 88.4% | 42.3% |

Break-even: 1 Pro weekly user covers all infrastructure + $100 profit/week.

### Internal Guardrails (invisible to user)
- **No daily application limit.** Users can apply as much as they want. No artificial pace cap.
- **API cost ceiling (60% rule):** Each user's API costs are tracked per billing cycle. When a user's accumulated API costs reach 60% of their subscription price, the bot silently switches from Pro mode to Starter mode (static resume/CL) for the remainder of the billing period. The user is NOT notified. Applications continue uninterrupted. The application feed still labels these as "Pro" to maintain the illusion.
  - Pro Weekly ($159.99): cap at $95.99 in API costs = ~1,247 tailored applications before silent switch
  - Pro Monthly ($499.99): cap at $299.99 in API costs = ~3,896 tailored applications before silent switch
  - Starter plans: effectively unlimited (cost per app is $0.002, cap would be ~3,000-15,000 apps)
- **Marketed as:** "Unlimited applications" on both tiers
- **Seek Quick Apply only:** The bot ONLY scrapes and applies to Seek Quick Apply jobs. External apply jobs are never scraped, never stored, never shown. They do not exist in the system. No "skipped (external)" status exists.
- **Anomaly detection:** If a single user's bot starts consuming unusual resources, flag for operator review

### Starter Mode Behavior
- User uploads one static resume
- User either uploads their own cover letter OR the system generates one generic cover letter for free on first bot start
- Both documents are submitted as-is to every job. No per-job AI calls.

### Pro Mode Behavior
- For each matching job, Claude Sonnet 4.6 generates a tailored resume and cover letter based on the job description + user profile
- Claude Haiku 4.5 answers screening questions (when present, ~30% of jobs)
- GPT-4o-mini scores job match (0-100) before tailoring to avoid wasting tokens on poor matches

## Authentication & Seek Login

### Google OAuth (Platform Login)
- Sole authentication method. No email/password.
- Requested scopes: `openid`, `email`, `profile`, `gmail.readonly`
- `gmail.readonly` is required from day one because Seek uses email sign-in codes (no password login)
- Same Gmail access enables future application response tracking without re-prompting for permissions

### Seek Authentication Flow
Seek does not use traditional passwords. Login requires an email + a sign-in code sent to that email.

1. User provides their Seek email during onboarding (stored in DB, not encrypted -- it's just an email)
2. When the bot needs to log into Seek, it initiates Seek's sign-in flow with the user's Seek email
3. Seek sends a sign-in code to that email address
4. Bot uses Gmail API to read the sign-in code from the user's inbox (searches for emails from Seek)
5. Bot enters the code and completes login
6. Playwright session cookies are stored in `seek_credentials.session_state` for reuse
7. Session is reused until it expires, then the flow repeats

### Gmail Sign-in Code Retrieval
- Search query: emails from Seek containing "sign-in code" or "verification code" in the last 5 minutes
- Retry logic: 3 attempts over 90 seconds (email delivery can be delayed)
- If code not found after retries: pause bot, alert user "Seek sign-in code not received. Check your Gmail."
- If user's Seek email differs from their Google OAuth email: works fine as long as the sign-in code lands in the Gmail account they authenticated with

## Document Upload & Processing

### Upload Flow
1. Frontend requests pre-signed S3 upload URL from API
2. Frontend uploads file directly to S3 (no server bottleneck, max 10MB)
3. Frontend notifies API that upload is complete
4. API triggers server-side text extraction

### Text Extraction
- **PDF:** pdfplumber extracts text. If extraction returns empty/minimal text (scanned PDF/image), reject with: "We couldn't read your resume. Please upload a text-based PDF or DOCX."
- **DOCX:** python-docx extracts text
- Extracted text is saved to `profiles.profile_text` (used for AI matching and tailoring)

### AI Validation (Claude Haiku, ~$0.001 per upload)
After text extraction, a Claude Haiku call validates:
- Is this actually a resume? (not a random document)
- Does it contain identifiable sections (name, experience, skills, education)?
- If validation fails: warn the user "This doesn't look like a resume. Are you sure?" with option to proceed anyway or re-upload.

### Cover Letter Handling
- Users can upload their own cover letter during onboarding or later in Documents
- If no cover letter exists when the bot starts for the first time, auto-generate one using Claude Sonnet from the user's profile text
- Show the generated cover letter to the user briefly before starting: "Here's your AI-generated cover letter. You can edit it anytime in Documents."
- Starter mode: this cover letter is used for all applications (static)
- Pro mode: a new tailored cover letter is generated per job (the default is a fallback)

## Billing System

### Stripe Integration
- Products: Starter Weekly, Starter Monthly, Pro Weekly, Pro Monthly (4 Stripe prices)
- Subscription creation on plan selection after free trial
- Webhook events handled: `invoice.paid`, `invoice.payment_failed`, `customer.subscription.updated`, `customer.subscription.deleted`
- Idempotency keys on all Stripe operations to prevent double charges

### Payment Failure (card declined on renewal)
- **Immediate bot pause.** Dashboard shows: "Payment failed. Update your card to resume."
- Bot session set to `error` status with message "Payment failed"
- User directed to Stripe billing portal to update payment method
- Bot can be restarted immediately once payment succeeds

### Upgrade (Starter to Pro, mid-cycle)
- **Immediate and prorated.** Stripe calculates the remaining value of the current Starter period, applies it as credit toward the Pro price, and charges the difference.
- Bot switches to Pro mode instantly. AI typing preview appears, next applications are tailored.
- New billing cycle starts from the upgrade date.

### Downgrade (Pro to Starter)
- **Takes effect at end of current billing period.** User keeps Pro until their current week/month expires.
- Dashboard shows: "Your plan will switch to Starter on [date]."
- After period ends, bot switches to Starter mode. AI tailoring stops. Static resume/cover letter used.

### Free Trial Exhaustion
- If the 5th free application is in progress when the trial runs out, let it finish.
- After completion, dashboard shows paywall: full Pro dashboard grayed out with a prominent upgrade CTA.
- Bot cannot be restarted without selecting a plan.

### Refund Policy
- No refunds. Users keep access until their current billing period ends.
- Clearly stated in Terms of Service.

## Bot Pre-flight Checklist

Runs every time the user presses "Start." If any hard blocker fails, the bot won't start. The dashboard shows exactly what needs to be fixed.

| # | Check | Type | Failure Response |
|---|-------|------|-----------------|
| 1 | Active subscription or free apps remaining | Hard blocker | Paywall screen |
| 2 | Seek email entered | Hard blocker | "Enter your Seek email to continue" |
| 3 | Gmail access working (can read inbox) | Hard blocker | "Reconnect your Google account" |
| 4 | At least one resume uploaded + set as default | Hard blocker | "Upload a resume first" |
| 5 | Profile text exists | Hard blocker | "Add your profile text for AI matching" |
| 6 | At least one active search keyword | Hard blocker | "Add a search keyword" |
| 7 | Cover letter exists | Soft (auto-fix) | Generate one from profile via Claude Sonnet, show to user, then proceed |
| 8 | Seek session valid | Soft (auto-fix) | Auto-login using Gmail sign-in code flow |

Pre-flight runs in order. First failure stops the check and surfaces the fix. Once all checks pass, the portal button animation triggers and the bot starts.

## Database Schema

### users
| Column | Type | Notes |
|--------|------|-------|
| id | UUID PK | |
| email | VARCHAR UNIQUE | Google OAuth email (also used for Gmail API) |
| name | VARCHAR | |
| google_id | VARCHAR UNIQUE | Google sub claim |
| google_refresh_token | TEXT | Encrypted. For Gmail API access. |
| avatar_url | VARCHAR | |
| phone | VARCHAR | |
| plan | ENUM(free, starter, pro) | |
| free_apps_used | INT DEFAULT 0 | Tracks the 5 free apps |
| stripe_customer_id | VARCHAR | |
| created_at | TIMESTAMP | |
| updated_at | TIMESTAMP | |

### seek_credentials
| Column | Type | Notes |
|--------|------|-------|
| id | UUID PK | |
| user_id | UUID FK(users) | One per user |
| seek_email | VARCHAR | Seek login email (plain text, just an email) |
| session_state | JSONB | Playwright session cookies |
| is_valid | BOOLEAN DEFAULT true | False if session expired |
| last_verified | TIMESTAMP | |
| created_at | TIMESTAMP | |

### profiles
| Column | Type | Notes |
|--------|------|-------|
| id | UUID PK | |
| user_id | UUID FK(users) | |
| headline | VARCHAR | "Data Engineer, Cloud Architect" |
| skills | TEXT[] | Array of skill keywords |
| location | VARCHAR | Target job location |
| match_threshold | INT DEFAULT 50 | Minimum match % to apply |
| profile_text | TEXT | Full resume text for AI matching |
| created_at | TIMESTAMP | |
| updated_at | TIMESTAMP | |

### documents
| Column | Type | Notes |
|--------|------|-------|
| id | UUID PK | |
| user_id | UUID FK(users) | |
| type | ENUM(resume, cover_letter) | |
| filename | VARCHAR | |
| s3_key | VARCHAR | Path in S3 bucket |
| extracted_text | TEXT | Text extracted from PDF/DOCX |
| ai_validated | BOOLEAN DEFAULT false | Passed Claude Haiku resume check |
| is_default | BOOLEAN DEFAULT false | Active doc for applications |
| created_at | TIMESTAMP | |

### searches
| Column | Type | Notes |
|--------|------|-------|
| id | UUID PK | |
| user_id | UUID FK(users) | |
| keyword | VARCHAR | "Data Engineer" |
| location | VARCHAR | "Sydney" |
| sort_mode | ENUM(date, relevance) | |
| is_active | BOOLEAN DEFAULT true | |
| created_at | TIMESTAMP | |

### bot_sessions
| Column | Type | Notes |
|--------|------|-------|
| id | UUID PK | |
| user_id | UUID FK(users) | UNIQUE -- one session per user |
| status | ENUM(idle, running, paused, error) | |
| ec2_instance_id | VARCHAR | EC2 instance this user's context is running on |
| started_at | TIMESTAMP | |
| last_heartbeat | TIMESTAMP | Worker pings every 30s |
| error_message | TEXT | |
| quiet_hours_enabled | BOOLEAN DEFAULT false | |
| quiet_hours_start | TIME | e.g. 23:00 |
| quiet_hours_end | TIME | e.g. 06:00 |
| timezone | VARCHAR DEFAULT 'Australia/Sydney' | |
| updated_at | TIMESTAMP | |

### applications
| Column | Type | Notes |
|--------|------|-------|
| id | UUID PK | |
| user_id | UUID FK(users) | |
| job_url | VARCHAR | |
| job_title | VARCHAR | |
| company | VARCHAR | |
| board | VARCHAR DEFAULT 'seek' | |
| match_score | INT | 0-100 from AI scorer |
| match_reasoning | TEXT | |
| mode | ENUM(basic, pro) | Was this tailored? |
| resume_s3_key | VARCHAR | Generated resume (pro) |
| cover_letter_s3_key | VARCHAR | Generated CL (pro) |
| status | ENUM(queued, in_progress, applied, skipped, failed) | |
| failure_reason | TEXT | |
| created_at | TIMESTAMP | |
| applied_at | TIMESTAMP | |

### seen_jobs
| Column | Type | Notes |
|--------|------|-------|
| id | UUID PK | |
| user_id | UUID FK(users) | |
| job_url | VARCHAR | |
| seen_at | TIMESTAMP | |
| UNIQUE(user_id, job_url) | | No dupes per user |

### subscriptions
| Column | Type | Notes |
|--------|------|-------|
| id | UUID PK | |
| user_id | UUID FK(users) | |
| plan | ENUM(starter, pro) | |
| billing_cycle | ENUM(weekly, monthly) | |
| status | ENUM(active, cancelled, past_due) | |
| stripe_sub_id | VARCHAR | |
| current_period_start | TIMESTAMP | |
| current_period_end | TIMESTAMP | |
| created_at | TIMESTAMP | |

### usage_logs
| Column | Type | Notes |
|--------|------|-------|
| id | UUID PK | |
| user_id | UUID FK(users) | |
| application_id | UUID FK(applications) | |
| mode | ENUM(basic, pro) | |
| billed | BOOLEAN DEFAULT false | Was this a paid app? |
| created_at | TIMESTAMP | |

### api_cost_tracking
| Column | Type | Notes |
|--------|------|-------|
| id | UUID PK | |
| user_id | UUID FK(users) | |
| application_id | UUID FK(applications) | Nullable (for non-application costs like validation) |
| api_provider | ENUM(openai, anthropic) | |
| model | VARCHAR | "gpt-4o-mini", "claude-sonnet-4-6", etc. |
| input_tokens | INT | |
| output_tokens | INT | |
| cost_usd | DECIMAL(10,6) | Calculated cost |
| billing_cycle_start | TIMESTAMP | Links to current subscription period |
| created_at | TIMESTAMP | |

### stripe_webhook_events
| Column | Type | Notes |
|--------|------|-------|
| id | UUID PK | |
| stripe_event_id | VARCHAR UNIQUE | Stripe event.id for idempotency |
| event_type | VARCHAR | e.g. "invoice.paid" |
| processed_at | TIMESTAMP | |
| payload_hash | VARCHAR | For debugging |

## Dashboard Design

### Design Direction
- **Theme:** Light mode. Clean white (#f8f9fb) backgrounds, soft borders (#e4e7ed)
- **Accent color:** Electric blue (#2563eb). Trust, technology, reliability.
- **Typography:** Plus Jakarta Sans (800 for headings, 600 for labels, 500 for body). JetBrains Mono for data/numbers.
- **Style:** Geometric sans, clean tech. Inspired by Revolut light mode, Apple, 2026 dashboard trends.
- **Feel:** "Living, active feed." The user opens the dashboard and immediately sees the bot is working. Numbers ticking, particles flowing, radar scanning. "My money is working right now."

### Dashboard Variants by Plan

#### Free Trial Dashboard
- **Full Pro experience.** All animations active: radar sweep, particles, orbiting rings, AI typing preview.
- Prominent counter: "3 of 5 free applications used" with a progress ring
- After 5 apps exhausted: entire dashboard grays out with a frosted overlay. Center CTA: "Your trial is over. You applied to 5 jobs with AI-tailored resumes. Upgrade to keep going." Shows Starter vs Pro pricing cards.
- Applications feed still visible (read-only) so they can see what the bot did and feel the loss.

#### Starter Dashboard
- Same layout as Pro: stats, bot status, searches, application feed
- **AI typing preview replaced with upgrade nudge card:** "Upgrade to Pro -- see your resume rewritten for every job. Your current resume is being sent as-is." With a small preview of their static resume and a "See what Pro looks like" button that shows a sample tailored resume.
- Mode pills in application table show "Starter" instead of "Pro"
- Bot status shows "Starter -- Static Resume" instead of "Pro -- AI Tailored"
- Radar and particles still active (bot is still scanning and applying, just not tailoring)
- Nav badge shows "STARTER" instead of "PRO" in a neutral gray chip

#### Pro Dashboard
- Full experience as designed in v2 mockup
- All animations: radar, particles, orbiting rings, AI typing preview
- Nav badge shows "PRO" in blue accent chip
- Application detail view includes: tailored resume preview, tailored cover letter preview, match reasoning

### Navigation
- Pill-style nav tabs: Dashboard, Applications, Documents, Settings
- Active tab has white background + subtle shadow (lifted pill)
- Blue active indicator line beneath on desktop
- Plan badge chip + notification bell + avatar in nav-right

### Dashboard Layout

**Greeting section:**
- "Good afternoon, [Name]" with name in blue accent
- Subtitle contextual to plan:
  - Free: "You have 2 free applications remaining."
  - Starter: "Your bot has sent 18 applications today."
  - Pro: "Your bot has sent 18 applications today. Next one in ~12 minutes."

**Stats row (4 cards):**
- Today (green), This Week (blue), All Time (amber), Avg Match (purple)
- Numbers count up from 0 on page load (eased cubic animation, 1.2s)
- Colored bottom bar sweeps in on hover
- Cards lift with spring bounce on hover, press-down on click

**Two-column section:**

*Bot Status card (left):*
- Radar visualization: rotating blue beam with blinking dots (discovered jobs)
- Orbiting rings: dashed and solid rings with satellite dots circling the radar
- Particle stream: continuous flow of blue/purple dots left-to-right (data pipeline)
- AI typing preview (Pro mode only): live JetBrains Mono text with blinking cursor showing resume being written
- Upgrade nudge card (Starter mode only): replaces AI typing section
- Bot details: last applied job, session uptime
- Progress bar: applications today (no cap, just a running count) with shimmer effect
- Portal button (see Animations section)

*Active Searches card (right):*
- List of keyword + location combos
- Green/amber dot per search (active/paused)
- Mode chip (date/relevance)
- "+ Add Search" button with spring hover
- Items slide right on hover

**Applications feed (full width):**
- Table: Position, Company, Match %, Mode, Status
- Color-coded left border on hover (green for applied, red for failed, gray for skipped)
- Ripple effect on click
- "View all" link to full Applications page

### Animations

**1. Radar sweep (ambient -- bot running state):**
- Rotating blue beam inside a circle
- Blinking dots appear/fade representing discovered jobs
- Always visible when bot is running (all plans)

**2. Particle stream (active -- bot applying):**
- Tiny blue/purple dots flowing left to right
- Continuous when bot is active
- Speed increases during application submission

**3. Orbiting rings (ambient -- status icon):**
- Concentric dashed/solid rings rotating around the radar
- Small satellite dots on ring edges
- Constant subtle rotation

**4. AI typing preview (Pro mode only):**
- JetBrains Mono text appearing character by character
- Blinking blue cursor
- "AI Writing Resume" label with pulsing dots
- Shows live preview of the tailored resume being generated
- Only visible in Pro mode and during free trial

**5. Portal button transition (state changes):**
- **Start/Resume:** Button collapses (squish, shrink to circle, disappear). Portal rings spin into existence with particles flying inward. Center glow pulses blue. New button emerges outward from center with particles exploding out + success ring.
- **Pause:** Same collapse with red glow/particles. Re-emerges as "Resume Bot."
- **Stop:** Collapses with gray glow. Re-emerges as "Start Bot."
- Every state transition = button consumed by portal, reborn as new state.
- Button is disabled during animation to prevent double-clicks.

**6. Counter roll-up (page load):**
- All stat numbers animate from 0 to target value
- Eased cubic timing, 1.2s duration
- Staggered delays per card (0.04s increments)

**7. Micro-interactions:**
- Button radial glow: light follows cursor position on hover
- Row ripple: click emanates a blue ripple from click point
- Spring bounce: cards, buttons, search items use cubic-bezier(0.34, 1.56, 0.64, 1)
- Staggered fade-up: page elements enter sequentially on load
- Progress bar shimmer: traveling white highlight on the pace bar

### Pages

**Dashboard (described above):** Home view with plan-specific variants.

**Applications:** Full paginated list with filters (status, date range, match score range, search keyword). Click any row to see:
- Job details (title, company, URL, match score, reasoning)
- Tailored resume preview (Pro only, rendered inline)
- Tailored cover letter preview (Pro only, rendered inline)
- Starter users see their static resume/CL with upgrade nudge

**Documents:**
- Upload/manage resumes and cover letters
- Set default document
- View previously generated tailored documents (Pro)
- Preview PDFs inline
- AI validation badge on each document ("Verified resume" or "Validation warning")
- Upload constraints: max 10MB, PDF or DOCX only

**Settings:**
- Profile: name, headline, skills, location, match threshold, profile text
- Seek: Seek email (editable), session status indicator
- Google: connected account, Gmail access status, reconnect button
- Billing: current plan, usage count, upgrade/downgrade, billing history, Stripe portal link
- Bot Configuration: search keywords (also on dashboard), daily schedule preferences
- Account: delete account (with confirmation and data deletion)

## Onboarding Flow

1. **Landing page** with pricing cards (weekly/monthly toggle). "Start Free -- 5 AI Applications" CTA.
2. **Sign up with Google** (one click). Requests `openid`, `email`, `profile`, `gmail.readonly` scopes. User sees Google consent screen explaining Gmail read access.
3. **Gmail + Seek email alignment page** (Step 0). Recommends creating a dedicated Gmail for job search and updating Seek email to match. Options: "Set up a new Gmail account" (links to Gmail signup) or "Skip -- I'll use my current email." See Onboarding -- Gmail + Seek Email Alignment section for full details.
4. **Enter Seek email** (the email they use to log into Seek). Validates whether it matches authenticated Gmail. Warning if mismatch.
5. **Upload resume** (required, PDF or DOCX, max 10MB). AI validation runs. Text extracted for profile.
6. **Optionally upload cover letter** ("Skip this -- we'll generate one for you when you start")
7. **Review profile text** (auto-populated from resume extraction, editable)
8. **Add first search keyword** (keyword + location + sort mode)
9. **Start free trial** (5 Pro applications). Pre-flight checklist runs. If no cover letter, one is auto-generated and shown before starting.
10. After 5 free apps: paywall with plan selection (Starter/Pro, weekly/monthly)

## Edge Cases & Failure Handling

### Bot Execution Failures

| Scenario | Response |
|----------|----------|
| Seek changes HTML/form structure -- bot can't find elements | Auto-pause. Alert: "Seek updated their site, our team is on it." Log for engineering. |
| Seek rate-limits or blocks account -- CAPTCHA appears | Auto-pause. Alert: "Seek flagged unusual activity. Bot paused to protect your account." |
| Bot crashes mid-application -- ECS task dies | Heartbeat missed for 60s. API marks session as error. Application marked as failed. User can restart. |
| Job listing removed/expired between scraping and applying | Mark as "skipped (expired)". Move to next job. No alert (normal). |
| Resume upload to Seek fails (file too large, wrong format) | Retry once. If still fails, mark application as failed with reason. Move to next. |

### Billing Edge Cases

| Scenario | Response |
|----------|----------|
| Payment fails on renewal (card declined) | Immediate bot pause. Dashboard: "Payment failed. Update your card to resume." |
| Free trial runs out mid-application (#5 in progress) | Let it finish. Then paywall. |
| Subscription expires while bot is running | Pause bot immediately. Show upgrade screen. |
| User wants refund mid-week/month | No refunds. Access continues until period ends. Stated in ToS. |
| Stripe webhook fires twice (double charge risk) | Idempotency keys on all Stripe operations. |
| Upgrade Starter to Pro mid-cycle | Immediate. Stripe prorates: credits remaining Starter value, charges Pro difference. New cycle starts. |
| Downgrade Pro to Starter | Takes effect at end of current period. Pro features retained until then. |

### Auth/Credential Failures

| Scenario | Response |
|----------|----------|
| Google OAuth token expires | Refresh silently using stored refresh token. |
| Google refresh token invalidated (user changed password) | Dashboard: "Reconnect your Google account." Bot paused. |
| Gmail can't find Seek sign-in code (email delayed) | Retry 3 times over 90 seconds. If still missing, pause. Alert: "Seek sign-in code not received." |
| User's Seek email differs from Google email | Works fine. Gmail reads inbox for codes sent to any email Seek sends codes to. The user's Google account just needs to receive those emails. |
| Seek session cookies expire | Pre-flight auto-fix: re-login using Gmail sign-in code flow. |

### Data Edge Cases

| Scenario | Response |
|----------|----------|
| User uploads a scanned PDF (image, no extractable text) | Reject: "We couldn't read your resume. Upload a text-based PDF or DOCX." |
| User uploads a non-resume document | AI validation warns: "This doesn't look like a resume." Option to proceed anyway or re-upload. |
| User uploads file over 10MB | Reject client-side before upload begins. |
| Two bot workers start for same user | Mutex lock on user_id in bot_sessions (UNIQUE constraint). Second worker exits immediately. |
| Same job appears in multiple search keywords | `seen_jobs` UNIQUE(user_id, job_url) prevents double-applying. |

### Concurrency Edge Cases

| Scenario | Response |
|----------|----------|
| User clicks Start twice rapidly | Portal button disabled during animation. API start endpoint is idempotent. |
| User changes settings while bot is running | Bot picks up new settings on next scrape cycle, not mid-application. |
| Multiple browser tabs open | WebSocket keeps all tabs in sync for bot status and application feed. |
| User deletes their default resume while bot is running | Bot finishes current application. On next application, pre-flight fails. Bot pauses with "No default resume set." |

## Security

- Seek credentials: only email stored (plain text). No passwords. Session cookies stored in DB (JSONB).
- Google refresh tokens: encrypted at rest, encryption key in AWS Secrets Manager
- Google OAuth: `gmail.readonly` scope. We only query for Seek-related emails. Documented clearly for users.
- S3: private bucket, pre-signed URLs for document access (URLs expire after 15 minutes)
- API: JWT tokens from Google OAuth, refresh token rotation
- Rate limiting: API endpoints rate-limited per user
- HTTPS everywhere
- Bot worker isolation: each user's bot runs in its own BrowserContext (Playwright context-level isolation within shared EC2 instances). Contexts have separate cookies, storage, and cache.

## Complete Page Map

### Public Pages (no auth required)
| Page | Purpose |
|------|---------|
| **Landing page** | Hero section, feature highlights, pricing cards (weekly/monthly toggle), testimonials, "Start Free" CTA |
| **Pricing page** | Detailed plan comparison: Free vs Starter vs Pro. Feature matrix. FAQ. Weekly/monthly toggle. |
| **Terms of Service** | No refunds policy, Seek automation disclaimer, data handling, liability |
| **Privacy Policy** | Gmail access explanation ("we only read Seek sign-in codes"), data encryption, S3 storage, retention policy |

### Auth Pages
| Page | Purpose |
|------|---------|
| **Login / Signup** | Single page. Google OAuth button. Value proposition sidebar. Redirects to dashboard if already authenticated, or onboarding if new user. |
| **Onboarding wizard** | Multi-step: (0) Gmail + Seek email alignment recommendation, (1) Enter Seek email + validation, (2) Upload resume + AI validation, (3) Optional cover letter upload, (4) Review profile text, (5) Add first search keyword, (6) Start free trial |

### Dashboard Pages (auth required)
| Page | Purpose |
|------|---------|
| **Dashboard** | Home view. Plan-specific variant (Free/Starter/Pro). Stats, bot status, searches, recent applications. |
| **Applications list** | Full paginated list. Filters: status, date range, match score, search keyword. Sortable columns. |
| **Application detail** | Click-through from list. Job details, match reasoning. Pro: tailored resume + cover letter preview. Starter: static doc + upgrade nudge. |
| **Documents** | Upload/manage resumes and cover letters. Set default. AI validation badge. Preview PDFs inline. Max 10MB, PDF/DOCX only. |
| **Settings - Profile** | Name, headline, skills, location, match threshold, profile text (editable) |
| **Settings - Seek** | Seek email (editable), session status indicator, "Test Connection" button |
| **Settings - Google** | Connected Google account, Gmail access status, reconnect button |
| **Settings - Billing** | Current plan + billing cycle, usage count, upgrade/downgrade options, billing history, link to Stripe Customer Portal |
| **Settings - Account** | Delete account (with confirmation + data deletion warning) |

### Overlay / Modal States
| State | Purpose |
|-------|---------|
| **Paywall (post-trial)** | Frosted overlay on grayed dashboard. "Your trial is over" + pricing cards. Shown after 5 free apps. |
| **Upgrade modal** | Plan selection cards when upgrading Starter to Pro. Shows prorated cost. |
| **Payment failed banner** | Persistent top banner: "Payment failed. Update your card to resume." Links to Stripe portal. |
| **Reconnect Google** | Modal: "Your Google connection expired. Reconnect to continue." OAuth re-auth button. |
| **Pre-flight checklist** | Modal before bot start. Shows each check with pass/fail. Blocks start until all hard blockers resolved. |
| **Cover letter generation** | Modal on first bot start if no CL exists. Shows AI-generated cover letter for review before proceeding. |

### Error Pages
| Page | Purpose |
|------|---------|
| **404** | "Page not found" with link back to dashboard |
| **Generic error** | "Something went wrong" with retry + support contact |

**Total: 16 pages + 6 overlay states + 2 error pages = 24 distinct UI states**

## Stripe AU Integration Details

### Setup Requirements
- Australian Business Number (ABN): registered
- GST registration: required (charging AU customers)
- Stripe AU account with AUD as default currency
- Quarterly BAS filing for GST collected

### Fee Structure
| Transaction Type | Fee | On $159.99 Pro Weekly |
|-----------------|-----|----------------------|
| Australian cards | 1.75% + $0.30 | $3.10 (1.9%) |
| International cards | 2.9% + $0.30 | $4.94 (3.1%) |
| Stripe Billing (subscriptions) | +0.5% | +$0.80 |

Note: Stripe Billing adds 0.5% for subscription management. Total effective rate for AU subscriptions: ~2.25% + $0.30.

### GST Handling
- All prices displayed to Australian customers include 10% GST
- Stripe Tax add-on calculates GST automatically on checkout
- GST amount recorded per invoice for BAS reporting
- Quarterly BAS filing required (can be automated with accounting software like Xero)
- International customers: no GST charged (export exempt)

### Stripe Products (4 prices)
| Product | Stripe Price ID | Interval | Amount (inc GST) |
|---------|----------------|----------|-----------------|
| Starter Weekly | price_starter_weekly | week | $19.99 AUD |
| Starter Monthly | price_starter_monthly | month | $49.99 AUD |
| Pro Weekly | price_pro_weekly | week | $159.99 AUD |
| Pro Monthly | price_pro_monthly | month | $499.99 AUD |

### Webhook Events Handled
| Event | Action |
|-------|--------|
| `invoice.paid` | Activate/renew subscription. Update `subscriptions.status` to active. Unblock bot if previously paused for payment. |
| `invoice.payment_failed` | Pause bot immediately. Set `bot_sessions.status` to error. Show payment failed banner. |
| `customer.subscription.updated` | Handle plan changes (upgrade/downgrade). Update `subscriptions.plan` and `billing_cycle`. |
| `customer.subscription.deleted` | Cancel subscription. Pause bot. Set plan to free. |
| `checkout.session.completed` | New subscription created. Activate plan. Remove paywall. |

### Idempotency
- All Stripe API calls use idempotency keys (Stripe-Idempotency-Key header)
- Webhook handler checks `event.id` against processed events table to prevent double-processing
- Critical: prevents double charges on retry scenarios

### Stripe Customer Portal
- Users manage their own payment methods, view invoices, and cancel subscriptions through Stripe's hosted portal
- Accessed via "Manage Billing" button in Settings
- Reduces support burden -- Stripe handles card updates, invoice PDFs, cancellation flow

## Monitoring & Alerting (Operator)

### Error Tracking
- **Sentry** (free tier: 5K errors/month) for all application errors across FastAPI and bot workers
- Every ECS task (API + workers) reports to Sentry with user context (user_id, plan, bot session)
- Source maps uploaded for frontend error tracking on Vercel

### CloudWatch Metrics
- ECS task CPU/memory utilization
- RDS connection count, query latency, storage
- S3 request counts
- Custom metrics: active bot workers, applications/hour, API response times

### Alert Triggers (notify operator via email + Slack)
| Trigger | Threshold | Severity |
|---------|-----------|----------|
| Bot worker crash (EC2 instance or worker process died) | Any occurrence | High |
| Seek structure change (3+ bots fail same selector in 10 min) | 3 failures | Critical |
| Payment failure spike | >5 failures in 1 hour | High |
| API error rate | >5% of requests in 5 min | High |
| RDS storage >80% | Threshold | Medium |
| Worker heartbeat missed >5 users simultaneously | 5 users | Critical |
| Sentry new error type | Any new unhandled exception | Medium |

### Operator Dashboard (v1 -- minimal)
- No custom admin UI at launch. Use:
  - Sentry dashboard for errors
  - CloudWatch dashboards for infrastructure
  - Stripe dashboard for revenue/billing
  - Direct database queries (pgAdmin or DBeaver) for user management
- Future: custom admin panel with user management, bot oversight, revenue analytics

## Email Notifications (AWS SES)

### Provider
- AWS SES ($0.10 per 1,000 emails). Already on AWS infrastructure.
- Sender domain: noreply@seekautoapply.com (requires DNS verification)
- HTML email templates stored in codebase, rendered server-side

### Email Types
| Email | Trigger | Content |
|-------|---------|---------|
| **Welcome** | After signup + onboarding complete | "Welcome to SeekAutoApply! Your bot is ready to go." Quick links to dashboard. |
| **Trial ended** | After 5th free application completes | "Your trial is over. You applied to 5 jobs with AI-tailored resumes. Here's what happened: [stats]. Upgrade to keep going." Pricing cards. |
| **Payment receipt** | After each successful charge | Handled by Stripe automatic receipts. No custom email needed. |
| **Payment failed** | invoice.payment_failed webhook | "Your payment failed. Your bot has been paused. Update your card to resume." Direct link to Stripe portal. |
| **Weekly summary** | Every Monday 9am AEST | "This week: X applications, top match: [job] at [company] (Y%). Your bot applied to Z companies." Mini stats + link to dashboard. |
| **Milestone celebration** | 50, 100, 250, 500 applications | "You've hit [N] applications! Here's your journey so far: [stats]." Encouraging, shareable. |
| **Seek session recovery failed** | After 3 failed auto-login attempts | "We couldn't log into your Seek account. Check your dashboard." Last resort only -- auto-recovery via Gmail handles 99% of cases. |

### Email Frequency Limits
- Maximum 1 transactional email per event (no duplicates via idempotency)
- Weekly summary: 1 per week, skipped if user had 0 applications that week
- Milestone: max 1 per week (don't spam if they pass 50 and 100 in same week)

## API Cost Tracking & 60% Rule

### How It Works
Every API call (Claude, GPT-4o-mini) is tracked per user per billing cycle.

| API Call | Cost Tracked |
|----------|-------------|
| GPT-4o-mini job scoring | ~$0.000285 per call |
| Claude Sonnet resume tailoring | ~$0.042 per call |
| Claude Sonnet cover letter generation | ~$0.033 per call |
| Claude Haiku screening Q&A | ~$0.0036 per call |
| Claude Haiku resume validation (upload) | ~$0.001 per call |
| Claude Sonnet cover letter auto-generation | ~$0.033 (one-time) |

### Database Table: api_cost_tracking
| Column | Type | Notes |
|--------|------|-------|
| id | UUID PK | |
| user_id | UUID FK(users) | |
| application_id | UUID FK(applications) | Nullable (for non-application costs like validation) |
| api_provider | ENUM(openai, anthropic) | |
| model | VARCHAR | "gpt-4o-mini", "claude-sonnet-4-6", etc. |
| input_tokens | INT | |
| output_tokens | INT | |
| cost_usd | DECIMAL(10,6) | Calculated cost |
| billing_cycle_start | TIMESTAMP | Links to current subscription period |
| created_at | TIMESTAMP | |

### Currency Handling
- Subscription prices are in AUD. API costs are tracked in USD.
- The 60% rule converts subscription price to USD using a daily exchange rate (cached, updated once per day via free API like exchangerate.host).
- Example: Pro Weekly $159.99 AUD at 0.65 AUD/USD = ~$103.99 USD. 60% cap = ~$62.40 USD in API costs.
- Exchange rate stored in a simple `exchange_rates` table or environment config. Updated daily by a scheduled task.

### Silent Mode Switch Logic
```
On each Pro application:
  1. Calculate cumulative API costs (USD) for current billing cycle
  2. Get user's subscription price (AUD), convert to USD using cached exchange rate
  3. If cumulative_costs_usd >= subscription_price_usd * 0.60:
     - Switch to Starter mode for this application (use static resume/CL)
     - Label the application as "Pro" in the database anyway
     - Do NOT notify the user
     - Continue applying without interruption
  4. On next billing cycle renewal: reset cumulative costs, restore Pro mode
```

## Data Retention & S3 Lifecycle

### Retention Policy
| Data | Retention | Action on Expiry |
|------|-----------|-----------------|
| Applications + job details (DB) | Forever | Kept for analytics |
| Generated resumes/CLs (S3) | Tiered (see below) | Lifecycle transitions then deletion |
| Seek session cookies (DB) | Until expiry | Overwritten on re-login |
| Usage logs (DB) | 1 year | Soft delete after 1 year |
| API cost tracking (DB) | 1 year | Archived then deleted |
| User account data | Until deletion requested | Hard delete within 30 days |

### S3 Lifecycle Policy (automatic, zero code)
| Age | Storage Class | Cost Relative |
|-----|--------------|---------------|
| 0-30 days | S3 Standard | 100% (frequent access, user viewing recent apps) |
| 31-90 days | S3 Infrequent Access | ~50% cheaper |
| 91-365 days | S3 Glacier Instant Retrieval | ~68% cheaper, still accessible |
| 365+ days | Delete | $0 |

### Account Deletion (Privacy Act compliance)
When a user requests account deletion:
1. Immediately: cancel Stripe subscription, pause bot, revoke Google OAuth tokens
2. Within 30 days: hard delete all data across all tables (CASCADE), delete all S3 objects, remove Sentry user context
3. Confirmation email: "Your data has been permanently deleted."
4. Retain only: anonymized aggregate stats (total applications count, no PII)

## WebSocket Specification

### Technology
- FastAPI WebSocket endpoints
- One connection per authenticated user session
- JWT authentication on WebSocket upgrade

### Events Pushed to Frontend
| Event | Payload | Dashboard Effect |
|-------|---------|-----------------|
| `application.submitted` | {job_title, company, match_score, mode, status} | New row animates into feed, stat counters tick up |
| `application.failed` | {job_title, company, failure_reason} | Row appears with red status |
| `application.skipped` | {job_title, company, reason} | Row appears with gray status (low match only) |
| `bot.status_changed` | {status: running/paused/idle/error} | Radar/particles start or stop, portal button state |
| `bot.ai_typing` | {text_chunk} | AI typing preview updates with new text (Pro only) |
| `bot.progress_update` | {applications_today} | Progress bar and stat cards update |
| `cost.threshold_reached` | (internal only, not shown to user) | Silently switches rendering mode |

### Reconnection Strategy
1. WebSocket drops: auto-reconnect with exponential backoff (1s, 2s, 4s, 8s, max 30s)
2. After 5 consecutive failures: fall back to HTTP polling every 10 seconds
3. On reconnect: fetch full current state from REST API to sync (don't replay missed events)
4. Stale tab detection: if tab hidden >5 minutes, close WebSocket, reconnect on tab focus

### Multiple Tabs
- All tabs connected via separate WebSocket connections
- All receive same events simultaneously
- Bot control actions (start/stop/pause) are idempotent -- multiple tabs sending "start" doesn't cause issues

## Onboarding -- Gmail + Seek Email Alignment

### Recommended Setup Page (Step 0 of onboarding)
After Google OAuth, before Seek email entry, show a dedicated recommendation page:

**"Set up for best results"**

"SeekAutoApply needs to read Seek sign-in codes from your email. For the smoothest experience, we recommend:

1. Create a new Gmail account dedicated to your job search (e.g. yourname.jobsearch@gmail.com)
2. Log into Seek and change your email to this new Gmail address (Seek > Settings > Email)
3. Come back here and sign in with your new Gmail account

This keeps your job search separate and gives our bot uninterrupted access to Seek sign-in codes."

**[Set up a new Gmail account]** (links to Gmail signup)
**[Skip -- I'll use my current email]** (proceeds to Seek email entry)

### Validation During Onboarding
After user enters their Seek email:
1. Check if the Seek email matches the authenticated Gmail account
2. If match: proceed
3. If mismatch: warning -- "Your Seek email (user@outlook.com) is different from your Gmail (user@gmail.com). Seek sign-in codes need to arrive in your Gmail for the bot to log in. Make sure codes are forwarded, or update your Seek email to match."
4. User can proceed anyway (they might have forwarding set up) or go back to fix it

## Bot Scheduling

### Default Behavior
- Bot runs 24/7 when started. No schedule restrictions by default.

### Optional Quiet Hours
- User can set quiet hours in Settings > Bot Configuration
- Simple UI: toggle "Enable quiet hours" + two time pickers (start, end) + timezone selector
- Example: "Don't apply between 11:00 PM and 6:00 AM AEST"
- During quiet hours: bot stops applying but stays in "running" state. Dashboard shows "Sleeping until 6:00 AM" with a moon icon. Radar animation slows to a crawl.
- Bot resumes automatically when quiet hours end.

### Database
Quiet hours columns are included in the `bot_sessions` table in the main Database Schema section above.

## API Versioning

### Strategy
- **URL-based versioning:** All API endpoints prefixed with `/api/v1/`
- **Frontend sends version header:** `X-API-Version: 1` on every request
- **Backend supports N and N-1:** When v2 ships, v1 stays active for 2 weeks minimum
- **Non-breaking changes** (new fields, new endpoints) don't require a version bump
- **Breaking changes** (removed fields, changed response shapes) require a new version
- **Deprecation process:** Log warnings when v(N-1) is called, remove after 2 weeks with no traffic

### Frontend-Backend Contract
- OpenAPI spec auto-generated from FastAPI (built-in)
- Frontend uses generated TypeScript types from OpenAPI spec
- CI check: if OpenAPI spec changes in a breaking way, require version bump

## SEO, Analytics & Conversion Tracking

### Google Analytics 4
- Installed on all pages (landing, pricing, dashboard)
- Track: page views, session duration, bounce rate, user flow

### Conversion Funnel (GA4 custom events)
| Step | Event Name | Trigger |
|------|-----------|---------|
| 1 | `landing_page_view` | User visits landing page |
| 2 | `pricing_page_view` | User visits pricing page |
| 3 | `signup_started` | User clicks "Sign up with Google" |
| 4 | `signup_completed` | Google OAuth succeeds |
| 5 | `onboarding_step_N` | Each onboarding step completed (1-6) |
| 6 | `trial_started` | User starts free trial (first bot start) |
| 7 | `trial_app_N` | Each free application (1-5) |
| 8 | `paywall_shown` | Trial ended, paywall displayed |
| 9 | `plan_selected` | User clicks a plan on paywall |
| 10 | `payment_completed` | First successful payment |
| 11 | `first_paid_app` | First application after paying |

### SEO (Landing Page)
- Meta title/description optimized for "Seek auto apply", "automatic job applications Australia"
- Open Graph + Twitter Card meta tags for social sharing
- Structured data (JSON-LD): SoftwareApplication schema
- Sitemap.xml + robots.txt
- Page speed optimized (Vercel handles this well)

### Blog Section
- Located at seekautoapply.com/blog
- Built with Next.js MDX or a headless CMS (Contentlayer or similar)
- Initial content plan (filled over time):
  - "How to get more interviews on Seek"
  - "ATS-friendly resume tips for Australian job seekers"
  - "Why tailored resumes get 3x more callbacks"
  - "The hidden job market on Seek Quick Apply"
- Blog posts serve dual purpose: SEO traffic + email newsletter content

## Mobile Responsiveness

### Responsive Pages (mobile-friendly)
| Page | Mobile Behavior |
|------|----------------|
| **Dashboard** | Cards stack vertically. Animations simplified (static radar icon, no particles/orbiting rings). Stats in 2x2 grid. Portal button works. |
| **Application feed** | Table becomes card list (one card per application). Swipe for details. |
| **Bot controls** | Start/stop/pause fully functional. Portal animation simplified (scale down, fewer particles). |
| **Stats overview** | 2x2 grid instead of 4-column row. Counter animation retained. |
| **Settings** | Full access: profile, Seek email, billing, Google connection, quiet hours. Form inputs adapted for touch. |

### Desktop Only
| Page | Reason |
|------|--------|
| **Document upload/management** | File picker + drag-and-drop + PDF preview don't work well on mobile. Users upload once during onboarding (desktop). |
| **Onboarding wizard** | First-time setup with file uploads. Desktop experience. Mobile shows: "Complete setup on a computer for the best experience." |
| **Application detail with resume/CL preview** | PDF rendering on mobile is poor. Show job details + match score on mobile, but "View tailored resume on desktop" for the PDF. |
| **Full animation suite** | Radar sweep, particles, orbiting rings, AI typing preview -- desktop only. Battery and performance concern on mobile. |

### Mobile Navigation
- Bottom tab bar (Dashboard, Applications, Settings) instead of top nav pills
- Hamburger menu for secondary items (Documents -- shows "use desktop" message)
- Pull-to-refresh on dashboard and application feed

### Breakpoints
- Mobile: <768px
- Tablet: 768px-1024px (same as mobile layout but slightly more spacious)
- Desktop: >1024px (full experience)

## Bot Worker Architecture (EC2 Browser Pool)

### Overview
Instead of 1 container per user (Fargate), all users share EC2 instances running a Playwright browser pool. Each user gets an isolated BrowserContext (~30-50MB RAM) within a shared Chromium process.

### Instance Configuration
- **Instance type:** t3.xlarge (4 vCPU, 16GB RAM)
- **Cost:** ~$122/month per instance
- **Capacity:** ~200 concurrent users per instance (12GB usable after OS + Chromium overhead)
- **AMI:** Custom AMI with Playwright, Chromium, Python pre-installed. No cold start.
- **Docker image size concern eliminated:** Chromium is pre-baked into the AMI, not pulled per task.

### Auto-Scaling Group
- Minimum: 1 instance (handles first ~200 users)
- Scale out: when average RAM utilization > 80% for 5 minutes, add 1 instance
- Scale in: when average RAM utilization < 30% for 15 minutes, remove 1 instance (drain users first)
- Health check: custom endpoint on worker process, instance replaced if unhealthy

### User-to-Instance Assignment
- API maintains a mapping of user_id -> instance_id in `bot_sessions` table
- When user starts bot: API picks the instance with lowest current user count
- When user stops bot: BrowserContext is closed, RAM freed, mapping removed
- If instance becomes unhealthy: all users on that instance are reassigned to healthy instances

### BrowserContext Isolation
Each user's BrowserContext provides:
- Separate cookies and session storage (Seek session per user)
- Separate cache
- No cross-contamination between users
- Context crash does not affect other contexts on same instance

### Worker Process Architecture
Each EC2 instance runs a single Python worker process that:
1. Accepts commands from the API via internal HTTP endpoint (start user, stop user, pause user)
2. Manages a pool of BrowserContexts (one per active user)
3. Runs a scheduler loop per user (scrape -> score -> tailor -> apply -> wait -> repeat)
4. Sends heartbeats to API every 30 seconds (POST /api/internal/heartbeat)
5. Reports events to API (POST /api/internal/events) which triggers WebSocket pushes
6. Monitors memory usage and refuses new users if above 80% threshold

### Worker-to-API Communication (Internal HTTP)
Worker calls API over internal VPC networking (no public internet):

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/api/internal/heartbeat` | POST | Worker sends {instance_id, active_users, memory_pct} every 30s |
| `/api/internal/events` | POST | Worker sends application events (submitted, failed, skipped, ai_typing) |
| `/api/internal/seek-login` | POST | Worker requests Seek sign-in code retrieval via Gmail API (API has Google credentials) |
| `/api/internal/cost-check` | GET | Worker checks if user has hit 60% API cost cap before making AI calls |

All internal endpoints are authenticated with a shared internal API key (not exposed publicly). Security group restricts access to VPC only.

### Memory Leak Prevention
- Each BrowserContext has a max lifetime of 6 hours. After that, it's closed and recreated.
- Worker monitors per-context memory usage. If a single context exceeds 100MB, it's recycled.
- Weekly instance rotation: auto-scaling group replaces instances on a rolling schedule (one per week) to prevent long-running memory creep.

### Cost at Scale
| Active Users | Instances Needed | Monthly Cost | Cost Per User |
|-------------|-----------------|-------------|---------------|
| 10 | 1 | $122 | $12.20 |
| 50 | 1 | $122 | $2.44 |
| 100 | 1 | $122 | $1.22 |
| 200 | 1 | $122 | $0.61 |
| 500 | 3 | $366 | $0.73 |
| 1,000 | 5 | $610 | $0.61 |

With EC2 Spot Instances (60-70% discount): costs drop to ~$0.25/user/month at scale.

## Customer Support

### Support Channel
- **Email:** support@seekautoapply.com
- Link in dashboard footer, Settings page, and all transactional emails
- Auto-reply with ticket number on receipt

### SLA
- Response within 24 hours (solo operator at launch)
- Priority for Pro users (flagged by plan in email metadata)

### Support Pages
- Help link in nav footer on all pages
- FAQ section on pricing page (common questions: "How does the bot apply?", "Is my data safe?", "Can I get a refund?")
- Future: help center / knowledge base (not in v1)

## DNS & Domain Configuration

### Domains (registered on Porkbun)
| Domain | Purpose |
|--------|---------|
| seekautoapply.com | Primary domain (TBD: pending final decision) |
| seekautoapply.com.au | AU-specific (redirect or primary) |
| autoapply.com.au | Shorter alternative (redirect) |

### Configuration (to be finalized)
- DNS hosting: Porkbun (current) or transfer to Route 53 / Cloudflare for better control
- Primary domain: TBD -- user still deciding
- Non-primary domains: 301 redirect to primary
- Subdomains: staging.seekautoapply.com (Vercel preview), api.seekautoapply.com (ECS API)
- SSL: Vercel handles frontend SSL automatically. API uses ACM (AWS Certificate Manager) certificate on ALB.

## CORS Configuration

Frontend (Vercel) and API (ECS) are on different domains. CORS must be configured on FastAPI:

```
Allowed origins:
  - https://seekautoapply.com
  - https://www.seekautoapply.com
  - https://seekautoapply.com.au
  - http://localhost:3000 (development only)

Allowed methods: GET, POST, PUT, DELETE, OPTIONS
Allowed headers: Authorization, Content-Type, X-API-Version
Credentials: true (for cookie-based sessions if needed)
```

WebSocket connections also require origin validation on upgrade.

## Deployment & Environment Strategy

### Environments
| Environment | Frontend | API | Database | Purpose |
|-------------|----------|-----|----------|---------|
| **Development** | localhost:3000 | localhost:8000 | Local PostgreSQL | Local development |
| **Staging** | staging.seekautoapply.com (Vercel preview) | Staging ECS service | Staging RDS instance (separate from prod) | Pre-production testing |
| **Production** | seekautoapply.com (Vercel) | Production ECS service | Production RDS | Live users |

### Staging Bot Behavior (Sandbox Mode)
- Staging environment connects to Seek but uses a **dedicated test Seek account** (not a real user's account)
- Bot runs in "dry run" mode: scrapes real jobs, scores them, generates tailored resumes, but does NOT click the final "Submit Application" button
- This validates the entire pipeline end-to-end without actually applying
- Dry run flag: `DRY_RUN=true` environment variable on staging EC2 instances and ECS tasks
- Staging Stripe uses Stripe test mode (test API keys, test cards)

### Deployment Process
- **Frontend:** Push to main -> Vercel auto-deploys. Preview deployments on PRs.
- **API:** Push to main -> GitHub Actions builds Docker image -> pushes to ECR -> updates ECS service (rolling deployment, zero downtime)
- **Bot worker:** New AMI built via Packer with latest code. Auto-scaling group performs rolling update (launch new instances, drain old ones).
- **Database migrations:** Alembic (Python). Run before API/worker deployment. Backward-compatible migrations only (add columns, don't remove).

### Environment Variables
| Variable | Where | Notes |
|----------|-------|-------|
| `DATABASE_URL` | ECS task definition | PostgreSQL connection string |
| `GOOGLE_CLIENT_ID` | Vercel + ECS | Google OAuth app |
| `GOOGLE_CLIENT_SECRET` | ECS (Secrets Manager) | |
| `STRIPE_SECRET_KEY` | ECS (Secrets Manager) | |
| `STRIPE_WEBHOOK_SECRET` | ECS (Secrets Manager) | |
| `ANTHROPIC_API_KEY` | ECS (Secrets Manager) | Claude API |
| `OPENAI_API_KEY` | ECS (Secrets Manager) | GPT-4o-mini |
| `AWS_S3_BUCKET` | ECS task definition | Resume/CL storage |
| `SENTRY_DSN` | Vercel + ECS | Error tracking |
| `SES_SENDER_EMAIL` | ECS task definition | noreply@seekautoapply.com |
| `DRY_RUN` | ECS task definition | "true" on staging only |
| `ENCRYPTION_KEY_ARN` | ECS task definition | Secrets Manager ARN for Google refresh token encryption |

## Future Phases (not in v1)

- **Gmail response monitoring:** Surface "employer replied" or "interview invitation" in dashboard using existing Gmail access
- **LinkedIn + Indeed support:** Additional board scrapers and apply flows
- **Application response tracking:** Which jobs progressed to interview, response rate analytics
- **Mobile app:** React Native dashboard
- **Team/agency plan:** Manage multiple candidates under one account
- **Analytics dashboard:** Response rate trends, best-performing keywords, optimal application times, weekly email digest
