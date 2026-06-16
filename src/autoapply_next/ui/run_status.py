"""Run-state vocabulary + user-facing copy for the running bot.

RunState is the overall status the main window shows (distinct from the per-job
ProgressStage in engine/progress.py). Each state has a plain, reassuring status
line. This module also holds the three cooldown messages shown during the pacing
wait between submits, the held-queue "Waiting on you" prompt, and the onboarding
honesty line.

VOICE: clear, warm, and plain, for a non-technical user. These strings render in
uncertain or waiting moments, so they reassure first and have no cleverness. No em
dashes anywhere (house rule). Drafts here are written to be humanized in review.
"""

from __future__ import annotations

from enum import Enum


class RunState(str, Enum):
    """What the bot as a whole is doing right now."""

    IDLE = "idle"
    SEARCHING = "searching"
    APPLYING = "applying"
    COOLDOWN = "cooldown"            # deliberate pacing wait between submits
    WAITING_ON_YOU = "waiting_on_you"  # held screening questions need answers
    DAILY_LIMIT = "daily_limit"     # today's submission cap reached
    PAUSED = "paused"               # user paused; resumable
    STOPPED = "stopped"
    ERROR = "error"


# Acknowledged once during onboarding (checkbox label). The user attests they
# understand applications go out under their name with the details they provide.
HONESTY_LINE = (
    "I understand that AutoApply will apply to jobs on my behalf using the "
    "information I provide, and that every application is sent under my name."
)

# Shown when one or more screening questions are held and need the user.
WAITING_ON_YOU_PROMPT = (
    "A few job questions need your answer before AutoApply can continue. "
    "Once you reply, it will pick up right where it left off."
)

# Rotated during the pacing wait between submissions, so the app reads as
# deliberate rather than stuck. Reassurance first.
COOLDOWN_MESSAGES: tuple[str, ...] = (
    "Spacing out your applications so they look natural. This is normal and "
    "helps keep your account safe.",
    "Taking a short pause between applications. Nothing is wrong, this is by "
    "design.",
    "Waiting a little before the next one, the way a person would. Back to it "
    "shortly.",
)

# Per-state status lines. APPLYING optionally takes done/total counts.
_STATUS_LINES: dict[RunState, str] = {
    RunState.IDLE: "Ready when you are.",
    RunState.SEARCHING: "Looking for jobs that match what you are after.",
    RunState.APPLYING: "Applying to jobs for you.",
    RunState.COOLDOWN: "Pausing briefly between applications.",
    RunState.WAITING_ON_YOU: "Waiting on a few answers from you.",
    RunState.DAILY_LIMIT: (
        "That is all of today's applications done. AutoApply will start again "
        "tomorrow."
    ),
    RunState.PAUSED: "Paused. Press start whenever you are ready to continue.",
    RunState.STOPPED: "Stopped.",
    RunState.ERROR: (
        "Something went wrong, so AutoApply has stopped to be safe. Nothing was "
        "sent twice and your applications so far are saved."
    ),
}


def status_line(state: RunState, *, done: int | None = None, total: int | None = None) -> str:
    """The user-facing status line for `state`. For APPLYING, pass done/total to
    show progress (e.g. '... (3 of 12)'); other states ignore the counts."""
    base = _STATUS_LINES[state]
    if state is RunState.APPLYING and done is not None and total is not None:
        return f"{base[:-1]} ({done} of {total})."
    return base


def cooldown_message(index: int) -> str:
    """One of the three cooldown messages, rotating by index so a long wait does
    not show the same line every tick."""
    return COOLDOWN_MESSAGES[index % len(COOLDOWN_MESSAGES)]
