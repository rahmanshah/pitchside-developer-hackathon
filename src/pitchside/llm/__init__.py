"""Model client and accounting: Agent Framework chat clients (Foundry or fake) behind one guard.

See docs/DECISIONS.md entries 10 and 11 for why it is built this way.
"""

from .config import ENV_VARS, ROLE_ENV, ConfigError, LLMSettings, Role, load_settings

__all__ = ["ENV_VARS", "ROLE_ENV", "ConfigError", "LLMSettings", "Role", "load_settings"]
