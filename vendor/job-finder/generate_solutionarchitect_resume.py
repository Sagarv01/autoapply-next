"""Solution-Architect-flavoured CV (Word) for the HOBAN / Greenway ACT role.

Reuses the neutral DevOps/Cloud builder and only overrides the professional
summary to foreground solution architecture and the AWS Solutions Architect
credential. All experience bullets, titles, and facts stay accurate.
"""
from pathlib import Path

import generate_council_resume as base

base.SUMMARY = (
    "AWS-certified Solutions Architect (Associate) and Cloud / DevOps Engineer "
    "with 5+ years designing and delivering end-to-end cloud solution "
    "architectures in production. Translates business requirements into "
    "fault-tolerant, highly available, secure designs across multi-account AWS "
    "environments, including a large-scale Amazon Connect contact-centre "
    "platform serving 20+ airports across 4+ countries. Strong in architecture "
    "documentation and governance, real-time and event-driven system "
    "integration (Kinesis, SNS/SQS, API Gateway, Lambda), Infrastructure as "
    "Code (Terraform, CloudFormation), security (IAM, KMS, GuardDuty, Security "
    "Hub), and stakeholder engagement. Comfortable establishing architectural "
    "principles, non-functional requirements, and guardrails, and supporting "
    "vendor and solution evaluation."
)
base.OUT_DOCX = Path("output") / "SagarVerma_SolutionArchitect.docx"

if __name__ == "__main__":
    print(f"Resume DOCX: {base.render()}")
