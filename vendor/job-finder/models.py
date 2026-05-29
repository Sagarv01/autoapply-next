from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class JobListing:
    url: str
    title: str
    company: str
    board: str          # 'linkedin' | 'seek' | 'indeed'
    description: str
    posted_at: str = ""
    easy_apply: bool = False


@dataclass
class Application:
    url: str
    title: str
    company: str
    board: str
    match_score: int = 0
    match_reasoning: str = ""
    resume_file: str = ""
    cover_letter_file: str = ""
    status: str = "pending"   # 'skipped'|'in_progress'|'applied'|'failed'
    notes: str = ""
    timestamp: str = field(
        default_factory=lambda: datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    )
    failure_count: int = 0
