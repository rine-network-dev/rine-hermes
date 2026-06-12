"""Shared config/auth resolver for hermes-rine.

One place every tool + the adapter get their async ``RineClient`` from. The
resolution chain is the SDK's, untouched: ``RINE_CLIENT_ID``+``RINE_CLIENT_SECRET``
env → ``RINE_CONFIG_DIR`` env → ``~/.config/rine`` → ``cwd/.rine``. We do **not**
reimplement that chain — the SDK's ``RineClient`` resolves it when ``config_dir`` /
``api_url`` / ``agent`` are left at their defaults. This module only:

- declares the chain via ``RINE_ENV_VARS`` (metadata for ``requires_env`` hints);
- builds an async ``RineClient`` lazily, passing only non-``None`` overrides;
- maps typed ``RineError``s to clear LLM-facing strings (``_format_error``).

Side-effect-free import: nothing here touches the network, reads credentials, or
constructs a client at import time. Unlike the crewai sibling there is no ``BaseTool``
base — Hermes tools are plain async functions.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from rine import (
    AuthenticationError,
    ConfigError,
    MlsUnsupportedError,
    NotFoundError,
    RateLimitError,
    RineApiError,
    RineClient,
    RineError,
)


@dataclass(frozen=True)
class RineEnvVar:
    """Metadata describing one env var that feeds the SDK's resolution chain."""

    name: str
    description: str
    required: bool = False


# Declared so operators see which vars feed the SDK's resolution chain. All
# optional — credentials may come from any rung (env creds → config dir). NO
# RINE_TOKEN: the Python SDK has no such concept (Node/MCP-only); never accept it.
RINE_ENV_VARS: list[RineEnvVar] = [
    RineEnvVar(
        name="RINE_CLIENT_ID",
        description="Rine OAuth client id (hosted / secrets-manager auth).",
    ),
    RineEnvVar(name="RINE_CLIENT_SECRET", description="Rine OAuth client secret."),
    RineEnvVar(
        name="RINE_CONFIG_DIR",
        description="Override the Rine config dir (default ~/.config/rine then ./.rine).",
    ),
    RineEnvVar(
        name="RINE_API_URL",
        description="Rine API base URL (default https://rine.network).",
    ),
]


def build_client(
    *,
    config_dir: str | None = None,
    api_url: str | None = None,
    agent: str | None = None,
) -> RineClient:
    """Build an async ``RineClient``, passing only the non-``None`` overrides.

    Any override left ``None`` is omitted so the SDK resolves it via its own chain
    (env creds → ``RINE_CONFIG_DIR`` → ``~/.config/rine`` → ``cwd/.rine``). Shared by
    every tool handler and the adapter — one contract, lazily invoked.
    """
    kwargs: dict[str, Any] = {}
    if config_dir is not None:
        kwargs["config_dir"] = config_dir
    if api_url is not None:
        kwargs["api_url"] = api_url
    if agent is not None:
        kwargs["agent"] = agent
    return RineClient(**kwargs)


def _format_error(err: Exception) -> str:
    """Map a Rine error to a clear, LLM-facing string.

    Tool handlers return ``tool_error(_format_error(err))`` so the agent retries or
    continues rather than seeing a raw traceback. Never leaks ciphertext.
    """
    if isinstance(err, MlsUnsupportedError):
        return (
            "This group uses MLS encryption, which the Python side can't post to. "
            "Create a sender-key group or have the TypeScript side disable MLS."
        )
    if isinstance(err, AuthenticationError):
        return (
            "Rine auth failed — set RINE_CLIENT_ID/RINE_CLIENT_SECRET or onboard "
            "(`python -m hermes_rine.onboard`)."
        )
    if isinstance(err, NotFoundError):
        return f"Not found: {err.detail}. Try rine_discover to find the right handle."
    if isinstance(err, RateLimitError):
        return f"Rate-limited; retry after {err.retry_after}s."
    if isinstance(err, ConfigError):
        return str(err)
    if isinstance(err, RineApiError):
        return f"{err.status}: {err.detail}"
    return str(err)


__all__ = ["RINE_ENV_VARS", "RineEnvVar", "RineError", "_format_error", "build_client"]
