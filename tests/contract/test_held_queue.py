"""M-C: the unknown-question held queue.

When the engine hits a screening question it can't answer from the candidate's
facts, the app must NOT let it guess and submit. Instead the question is held:
queued once per unique question (deduped across jobs), the affected jobs are
blocked, and answering it unblocks every job that was only waiting on it. The
answer is remembered so the same question is never asked twice. State persists.
"""

from __future__ import annotations

from autoapply_next.screening.held_queue import HeldQueue, normalize_question


def test_normalize_dedups_case_and_whitespace_and_punctuation():
    a = normalize_question("How many years of AWS experience?")
    b = normalize_question("  how many years of aws experience  ?? ")
    assert a == b
    assert normalize_question("Do you have a police check*") == normalize_question("do you have a police check")


def test_hold_creates_a_waiting_entry():
    q = HeldQueue()
    entry = q.hold("job-1", "Why do you want this role?")
    assert entry.status == "waiting"
    assert "job-1" in entry.job_ids
    assert q.is_blocked("job-1")
    assert [e.question for e in q.pending()] == ["Why do you want this role?"]


def test_same_question_from_two_jobs_dedups_to_one_entry():
    q = HeldQueue()
    q.hold("job-1", "Do you have a current police check?")
    q.hold("job-2", "do you have a current police check")  # same after normalize
    pending = q.pending()
    assert len(pending) == 1
    assert set(pending[0].job_ids) == {"job-1", "job-2"}


def test_known_answer_is_none_until_answered_then_returned():
    q = HeldQueue()
    question = "What is your expected start date?"
    q.hold("job-1", question)
    assert q.known_answer(question) is None
    q.answer(question, "Immediately available")
    assert q.known_answer("  what is your EXPECTED start date? ") == "Immediately available"


def test_answer_unblocks_jobs_waiting_only_on_that_question():
    q = HeldQueue()
    q.hold("job-1", "Why this company?")
    q.hold("job-2", "Why this company?")
    unblocked = q.answer("Why this company?", "Your mission aligns with my values.")
    assert set(unblocked) == {"job-1", "job-2"}
    assert q.pending() == []
    assert not q.is_blocked("job-1")


def test_job_waiting_on_two_questions_unblocks_only_after_both_answered():
    q = HeldQueue()
    q.hold("job-1", "Question A?")
    q.hold("job-1", "Question B?")
    assert q.answer("Question A?", "a") == []          # still blocked by B
    assert q.is_blocked("job-1")
    assert q.answer("Question B?", "b") == ["job-1"]    # now fully unblocked
    assert not q.is_blocked("job-1")


def test_questions_for_job_lists_only_that_jobs_unanswered():
    q = HeldQueue()
    q.hold("job-1", "Q1?")
    q.hold("job-1", "Q2?")
    q.hold("job-2", "Q3?")
    qs = {e.question for e in q.questions_for_job("job-1")}
    assert qs == {"Q1?", "Q2?"}


def test_answering_unknown_question_is_a_noop():
    q = HeldQueue()
    assert q.answer("never asked", "x") == []


def test_options_and_input_type_are_recorded():
    q = HeldQueue()
    entry = q.hold("job-1", "Pick one", options=["A", "B"], input_type="single_select")
    assert entry.options == ["A", "B"]
    assert entry.input_type == "single_select"


def test_state_persists_round_trip(tmp_path):
    path = tmp_path / "held.json"
    q = HeldQueue()
    q.hold("job-1", "Q1?", options=["A"], input_type="single_select")
    q.hold("job-2", "Q2?")
    q.answer("Q1?", "A")
    q.save(path)

    loaded = HeldQueue.load(path)
    assert loaded.known_answer("Q1?") == "A"
    assert loaded.is_blocked("job-2")
    assert not loaded.is_blocked("job-1")
    assert {e.question for e in loaded.pending()} == {"Q2?"}


def test_load_missing_file_is_empty(tmp_path):
    loaded = HeldQueue.load(tmp_path / "nope.json")
    assert loaded.pending() == []
    assert loaded.known_answer("anything") is None
