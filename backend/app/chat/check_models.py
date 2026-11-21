r"""Check the model IDs in config.yaml against Anthropic's Models API (free; no tokens used).

Run: check_models.cmd   (or: backend\.venv\Scripts\python.exe -m app.chat.check_models)
Prints only model IDs and names. The key is read from .env and never printed.
"""

from __future__ import annotations

import sys


def main() -> int:
    from app.settings import get_config, get_secrets

    cfg = get_config()
    if cfg.llm is None:
        print("config.yaml has no llm section")
        return 1
    key = get_secrets().ANTHROPIC_API_KEY.get_secret_value().strip()
    if not key:
        print("ANTHROPIC_API_KEY: key not set (add it to .env)")
        return 1
    import anthropic

    client = anthropic.Anthropic(api_key=key, max_retries=1, timeout=30.0)
    bad = 0
    for tier, t in cfg.llm.tiers.items():
        try:
            m = client.models.retrieve(t.model)
            print(f"OK   {tier:<10} {t.model:<20} = {m.display_name}")
        except anthropic.NotFoundError:
            bad += 1
            print(f"FAIL {tier:<10} {t.model:<20} not found: fix the ID in config.yaml")
        except anthropic.AuthenticationError:
            print("FAIL the API key was rejected (check ANTHROPIC_API_KEY in .env)")
            return 1
        except anthropic.APIConnectionError:
            print("FAIL could not reach Anthropic (network)")
            return 1
        except anthropic.APIStatusError as exc:
            bad += 1
            print(f"FAIL {tier:<10} {t.model:<20} error {exc.status_code}")
    print("All model IDs found." if not bad else f"{bad} model ID(s) need fixing.")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
