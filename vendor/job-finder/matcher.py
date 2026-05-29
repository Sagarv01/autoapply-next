from claude_cli import DEFAULT_MODEL, claude_complete
from models import JobListing
from utils import extract_json, load_profile, retry_async

_SYSTEM = (
    "You are a job match evaluator. Given a candidate profile and a job "
    "description, score how well the candidate matches the requirements "
    "from 0 to 100. Return ONLY valid JSON with the exact shape: "
    '{"score": <int 0-100>, "reasoning": "<one short sentence>"}. '
    "No markdown fences, no preamble."
)


async def score_job(job: JobListing) -> tuple[int, str]:
    """Returns (score 0-100, one-sentence reasoning) via Claude on the Max subscription."""
    user = (
        f"CANDIDATE PROFILE:\n{load_profile()}\n\n"
        f"JOB TITLE: {job.title}\nCOMPANY: {job.company}\n\n"
        f"JOB DESCRIPTION:\n{job.description or '(none)'}"
    )

    async def _call():
        text = await claude_complete(system=_SYSTEM, user=user, model=DEFAULT_MODEL)
        try:
            data = extract_json(text)
            return int(data["score"]), str(data["reasoning"])
        except (KeyError, ValueError) as exc:
            raise ValueError(f"Unexpected matcher CLI response: {text!r}") from exc

    return await retry_async(_call)
