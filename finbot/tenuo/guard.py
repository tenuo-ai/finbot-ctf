"""Mint, narrow and check task-scoped warrants for FinBot agents.

Authority flows down a delegation chain:

- When a workflow starts, the orchestrator gets a root warrant built from the
  route's trusted IDs. It covers everything any agent in the workflow could
  be allowed to do for this vendor and invoice.
- When the orchestrator hands off, the sub-agent gets a narrower warrant
  attenuated from that root, built from the database facts at that moment
  (e.g. payment authority only if the invoice is approved by then). A child
  can never hold more than its parent; Tenuo refuses to mint it.
- Chat assistants get a warrant per conversation turn from the session.

Every tool call is checked against the whole chain before it runs.
"""

import inspect
import logging
import time
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Callable

from tenuo import Authorizer, SigningKey, Subset, Warrant

from finbot.core.auth.session import SessionContext
from finbot.tenuo.facts import Facts, load_facts
from finbot.tenuo.policies import (
    AGENT_POLICIES,
    CHAT_POLICIES,
    DELEGATES,
    Spec,
    merge,
)

logger = logging.getLogger(__name__)

WARRANT_TTL_SECONDS = 900
MAINTENANCE_PREFIX = "SYSTEM MAINTENANCE REQUEST"

# Stand-in for a control plane: one issuer key per process. Only this module
# can mint root warrants; agents only ever hold them.
_ISSUER_KEY = SigningKey.generate()
_AUTHORIZER = Authorizer(trusted_roots=[_ISSUER_KEY.public_key])


@dataclass
class _Workflow:
    """The root of the current workflow's delegation chain."""

    chain: list[Warrant]
    holder: SigningKey
    session_context: SessionContext
    vendor_id: int | None
    invoice_id: int | None


_WORKFLOW: ContextVar[_Workflow | None] = ContextVar("tenuo_workflow", default=None)


def _build(builder, s: Spec):
    for tool, constraints in s.items():
        builder = builder.capability(tool, **constraints)
    return builder


def _mint_root(s: Spec, holder: SigningKey) -> Warrant:
    return (
        _build(Warrant.mint_builder(), s)
        .holder(holder.public_key)
        .ttl(WARRANT_TTL_SECONDS)
        .mint(_ISSUER_KEY)
    )


def _attenuate(parent: Warrant, parent_holder: SigningKey, s: Spec, holder: SigningKey) -> Warrant:
    return _build(parent.grant_builder(), s).holder(holder.public_key).grant(parent_holder)


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value) if value not in (None, "", 0, "0") else None
    except (TypeError, ValueError):
        return None


class TenuoGuard:
    """One agent's warrant chain, its holder key, and the spec behind it."""

    def __init__(self, chain: list[Warrant], holder: SigningKey, s: Spec):
        self.chain = chain
        self.holder = holder
        self.spec = s

    @property
    def warrant(self) -> Warrant:
        return self.chain[-1]

    # --- Minting ------------------------------------------------------------

    @classmethod
    async def for_task(
        cls,
        agent_name: str,
        task_data: dict[str, Any] | None,
        session_context: SessionContext,
        agent_config: dict[str, Any],
    ) -> "TenuoGuard | None":
        """Warrant for an agent task, or None if the agent has no policy."""
        policy = AGENT_POLICIES.get(agent_name)
        if policy is None:
            return None
        task_data = task_data or {}

        if agent_name == "orchestrator_agent":
            return cls._mint_workflow(task_data, session_context, agent_config)

        workflow = _WORKFLOW.get()
        if workflow is not None:
            # IDs come from the workflow's trusted root, not from the
            # orchestrator model's tool arguments.
            facts = load_facts(session_context, workflow.vendor_id, workflow.invoice_id)
        else:
            facts = load_facts(
                session_context,
                _int_or_none(task_data.get("vendor_id")),
                _int_or_none(task_data.get("invoice_id")),
            )
        maintenance = (
            facts.is_admin
            and str(task_data.get("description", "")).startswith(MAINTENANCE_PREFIX)
        )
        s = policy(facts, agent_config, maintenance=maintenance)
        holder = SigningKey.generate()
        if workflow is None:
            warrant = _mint_root(s, holder)
            return cls([warrant], holder, s)
        try:
            child = _attenuate(workflow.chain[-1], workflow.holder, s, holder)
        except Exception:  # pylint: disable=broad-exception-caught
            # The task asked for more than the workflow holds. Fail closed:
            # the agent can only finish the task.
            logger.exception("Tenuo: %s spec exceeds the workflow root", agent_name)
            s = {"complete_task": {}}
            child = _attenuate(workflow.chain[-1], workflow.holder, s, holder)
        return cls([*workflow.chain, child], holder, s)

    @classmethod
    def _mint_workflow(cls, task_data, session_context, agent_config) -> "TenuoGuard":
        vendor_id = _int_or_none(task_data.get("vendor_id"))
        invoice_id = _int_or_none(task_data.get("invoice_id"))
        facts = load_facts(session_context, vendor_id, invoice_id)
        vendor_id = facts.vendor["id"] if facts.vendor else None
        invoice_id = facts.invoice["id"] if facts.invoice else None

        # The root covers the orchestrator's own tools plus the most any
        # delegate could be granted for this vendor and invoice.
        specs = [AGENT_POLICIES["orchestrator_agent"](facts, agent_config)]
        for name in DELEGATES:
            specs.append(AGENT_POLICIES[name](
                facts, _delegate_config(name, agent_config),
                ceiling=True, maintenance=facts.is_admin,
            ))
        root_spec = merge(specs)
        holder = SigningKey.generate()
        root = _mint_root(root_spec, holder)
        _WORKFLOW.set(_Workflow([root], holder, session_context, vendor_id, invoice_id))

        own = AGENT_POLICIES["orchestrator_agent"](facts, agent_config)
        orch_holder = SigningKey.generate()
        orch = _attenuate(root, holder, own, orch_holder)
        return cls([root, orch], orch_holder, own)

    @classmethod
    async def for_chat(cls, agent_name: str, session_context: SessionContext) -> "TenuoGuard | None":
        """Warrant for one chat turn, from the session."""
        policy = CHAT_POLICIES.get(agent_name)
        if policy is None:
            return None
        facts = load_facts(session_context, session_context.current_vendor_id)
        s = policy(facts, {})
        holder = SigningKey.generate()
        return cls([_mint_root(s, holder)], holder, s)

    # --- Checking -----------------------------------------------------------

    def prepare(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        defaults: dict[str, Any],
    ) -> tuple[str | None, dict[str, Any]]:
        """Check a call; return (denial reason or None, arguments to run with).

        Arguments the warrant constrains but the model left out are filled
        with the tool's own defaults first, since a constrained argument must
        be present. Running with the default is the same call the tool would
        have made anyway.
        """
        args = dict(arguments or {})
        for arg, constraint in self.spec.get(tool_name, {}).items():
            if arg not in args:
                if arg not in defaults:
                    continue
                args[arg] = defaults[arg]
            # An empty optional recipient list is sent as None or omitted;
            # both mean "nobody", the same as [].
            if args[arg] is None and isinstance(constraint, Subset):
                args[arg] = []

        signature = self.warrant.sign(self.holder, tool_name, args, int(time.time()))
        try:
            _AUTHORIZER.check_chain(self.chain, tool_name, args, bytes(signature))
        except Exception as e:  # pylint: disable=broad-exception-caught
            return str(e), args
        return None, args


def _delegate_config(agent_name: str, orchestrator_config: dict[str, Any]) -> dict[str, Any]:
    """The delegate's own configuration (its limits live there)."""
    # pylint: disable=import-outside-toplevel
    from finbot.agents.specialized.invoice import InvoiceAgent

    if agent_name == "invoice_agent":
        return InvoiceAgent._load_config(None)  # pylint: disable=protected-access
    return {}


def tool_defaults(callable_fn: Callable | None, input_schema: dict | None = None) -> dict[str, Any]:
    """Default argument values for a native callable or an MCP tool schema."""
    defaults: dict[str, Any] = {}
    if input_schema:
        for arg, prop in (input_schema.get("properties") or {}).items():
            if "default" in prop:
                defaults[arg] = prop["default"]
        return defaults
    if callable_fn is not None:
        try:
            for name, param in inspect.signature(callable_fn).parameters.items():
                if param.default is not inspect.Parameter.empty:
                    defaults[name] = param.default
        except (TypeError, ValueError):
            pass
    return defaults
