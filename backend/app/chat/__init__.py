"""AI chat (phase 2): Claude API calls, tools, usage/cost logging. Backend only; key never leaves it."""

import logging

# SDK / HTTP loggers stay quiet: request details must never reach the console or logs.
for _name in ("anthropic", "httpx2", "httpcore2", "httpx", "httpcore"):
    logging.getLogger(_name).setLevel(logging.WARNING)
