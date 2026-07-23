"""Flag submission tool."""

from pydantic_ai import RunContext

from backend.deps import SolverDeps
from backend.tools.core import do_submit_flag


async def submit_flag(ctx: RunContext[SolverDeps], flag: str) -> str:
    """Submit a flag to the configured platform to verify it. Always call this before reporting a flag.

    Returns CORRECT, ALREADY SOLVED, or INCORRECT.
    Do NOT submit placeholder flags like CTF{flag} or CTF{placeholder}.
    """
    if ctx.deps.no_submit:
        return f'DRY RUN — would submit "{flag.strip()}" but --no-submit is set.'

    # Use deduped submission via swarm if available, otherwise a direct platform call.
    if ctx.deps.submit_fn:
        display, is_confirmed = await ctx.deps.submit_fn(flag)
    else:
        display, is_confirmed = await do_submit_flag(
            ctx.deps.platform_client, ctx.deps.challenge_name, flag
        )
    if is_confirmed:
        ctx.deps.confirmed_flag = flag.strip()
    return display
