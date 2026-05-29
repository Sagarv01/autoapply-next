import os

# Set dummy env vars for all tests so imports don't fail
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")
os.environ.setdefault("OPENAI_API_KEY", "test-key")
os.environ.setdefault("MATCHER_MODEL", "gpt-4o-mini")
os.environ.setdefault("WRITER_MODEL", "claude-sonnet-4-6")
os.environ.setdefault("AGENT_MODEL", "claude-haiku-4-5-20251001")
