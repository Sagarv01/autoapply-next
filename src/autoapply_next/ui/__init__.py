"""UI package for AutoApply Next.

Seven screens, one main window. Each screen is a self-contained QWidget that
the MainWindow swaps into a QStackedWidget.

Screens:
  0. SignInScreen           Supabase phone + password
  1. SessionSetupScreen     One-time Seek session bootstrap
  2. ProfileScreen          Edit candidate.name/email/phone + resume profile text
  3. QueueScreen            Scrape + score jobs, queue selected ones
  4. RunScreen              Live progress for one application (the skeleton)
  5. ResultsScreen          History + per-job review (cover letter + screening answers)
  6. SettingsScreen         ALLOW_REAL_SUBMIT toggle (with warning), thresholds
"""
