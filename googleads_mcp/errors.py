"""The one place an exception becomes a message the agent can read.

The MCP runtime carries a ToolError's message through to the caller as
"Error executing tool <name>: <message>", and replaces EVERY other exception
with a bare "Error executing tool <name>", keeping the detail server-side. So an
exception that is not converted here tells the agent nothing: it cannot see
which bound it broke or that Google's quota ran out, and will most likely send
the same call again.

This used to be two copies (tools_read and tools_write) that disagreed about
which exceptions counted. Now there is one, and it converts everything:

  * deliberate refusals (a guardrail, a stale preview, the kill switch) pass
    through with their own wording, which names what to do;
  * Google API failures are prefixed with their class -- AUTH_EXPIRED, QUOTA,
    INVALID_QUERY, NOT_FOUND, POLICY (with the policy topic), TRANSIENT -- see
    googleads_reporting.errors;
  * anything else is a bug, and says UNEXPECTED with its type, rather than
    vanishing. The tool_calls log still records the traceback.
"""

from __future__ import annotations

import functools

from mcp.server.mcpserver.exceptions import ToolError

from googleads_reporting.errors import NOT_FOUND, classify, prefixed


def _message(exc: BaseException) -> str:
    if classify(exc) is not None:
        return prefixed(exc)
    if isinstance(exc, LookupError):
        # KeyError's str() is its repr, quotes and all.
        text = exc.args[0] if exc.args else str(exc)
        return f"{NOT_FOUND}: {text}"
    if isinstance(exc, (ValueError, RuntimeError)):
        # ValueError covers our argument refusals; RuntimeError covers
        # PreviewExpired, PreviewStale, WritesDisabled, MutationError and the
        # API wrappers -- all of which carry a message written for the reader.
        return str(exc)
    return f"UNEXPECTED: {type(exc).__name__}: {exc}"


def refusals_reach_the_caller(fn):
    """Re-raise anything a tool raises as ToolError, so its message survives."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except ToolError:
            raise
        except Exception as exc:  # noqa: BLE001 - converted, never swallowed
            raise ToolError(_message(exc)) from exc

    return wrapper
