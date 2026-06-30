# AutoApply Next Launch Week Checklist

Target: ship the PySide6 desktop app (`autoapply-next`) using the existing
`api.autoapply.com.au` proxy.

## Automated Checks

Run before every release candidate:

```bash
PYTHONPATH=. QT_QPA_PLATFORM=offscreen .venv/bin/pytest tests/ui_tests -q
PYTHONPATH=. QT_QPA_PLATFORM=offscreen .venv/bin/pytest tests/contract -q
```

The contract suite needs permission to bind `127.0.0.1` for the Stripe checkout
return tests.

Proxy checks:

```bash
cd ../autoapply.com.au/proxy-server
PYTHONPATH=. venv/bin/pytest -q
```

## Build Channels

End-user build, for non-technical users:

```bash
pyinstaller packaging/pyinstaller.spec
```

Tester build, with diagnostic/manual screens and advanced controls:

```bash
AUTOAPPLY_BUILD_AUDIENCE=tester pyinstaller packaging/pyinstaller.spec
```

End-user builds hide tester-only screens such as the raw Profile editor and
single-URL Run screen. Tester builds keep them visible.

Use `docs/END_USER_FEATURE_REMOVAL_CHECKLIST.md` as the release checklist for
what must stay out of the non-technical user build.

## Manual Desktop UI Pass

- Fresh workdir launch shows the branded setup flow and locked bot screens.
- Sign-in stays responsive on success, wrong password, and network failure.
- Profile, documents, criteria, and acknowledgement steps complete in order.
- Queue, Run, Batch, Results, Waiting on you, Upgrade, and Settings screens
  render without clipped text at the release window size.
- Tester build dry-run and live-submit badges are obvious and cannot be missed.
- End-user build has no real-submission gate, dry-run badge, live-submit badge,
  or engine workdir path.
- STOP remains reachable during batch runs.

## Live Dry-Run E2E

Run against a real Seek session with `allow_real_submit=False`.

- Fresh or pilot workdir has a valid Seek session.
- Scrape criteria returns relevant queued jobs.
- Score threshold is honored.
- One quick-apply job reaches submit-ready.
- SafetyGate captures screenshot and prevents submission.
- Results show generated documents and screening Q&A.
- No duplicate/same-role guard false positives.

## Live Submit Pilot

Requires explicit owner approval because it sends real applications.

- Cap at 5 jobs.
- Use production proxy URL.
- Confirm Supabase auth token and entitlement status before starting.
- Keep throttle/pacing enabled.
- Halt on any failed, uncertain, held, captcha, auth, or verifier result.
- Verify every submitted job through all available checks:
  submit click result, RobustVerifier, Seek applied page, confirmation email,
  SQLite row, and run tally.

## Release Gates

- Production proxy health and `/api/config` reachable.
- Stripe live price IDs configured.
- Supabase secret key provisioned for webhook writes.
- Version floor does not block the release build.
- Packaged macOS build launches on a clean workdir.
- Windows build/smoke is either completed or explicitly deferred.
- Code signing/notarization status is recorded in `HANDOFF.md`.
