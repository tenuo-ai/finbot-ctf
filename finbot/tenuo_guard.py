"""Tenuo task-scoped warrants for FinBot agents (defended mode).

When TENUO_ENFORCE is on, trusted platform code mints a warrant at the start
of each agent task. The warrant lists the tools the agent may call for that
task and the argument values it may use, derived from the database record the
task is about, never from the prompt. Every tool call is checked against the
warrant before it runs. A denied call never executes; the agent gets an error
back instead.

The model can still be manipulated. What changes is that a manipulated model
can only do what the task's warrant allows.
"""

import logging
import time
from typing import Any, Awaitable, Callable

from tenuo import Authorizer, Exact, OneOf, Range, SigningKey, Warrant, Wildcard

from finbot.core.auth.session import SessionContext
from finbot.tools import get_invoice_details, get_vendor_details

logger = logging.getLogger(__name__)

WARRANT_TTL_SECONDS = 600

# Stand-in for a control plane: one issuer key per process. Only code in this
# module can mint warrants; agents only ever hold them.
_ISSUER_KEY = SigningKey.generate()
_AUTHORIZER = Authorizer()
_AUTHORIZER.add_trusted_root(_ISSUER_KEY.public_key)

# Capabilities are (tool_name, {arg_name: constraint}). An empty dict means the
# tool may be called with any arguments.
Capabilities = list[tuple[str, dict[str, Any]]]
Policy = Callable[
    [dict[str, Any], SessionContext, dict[str, Any]], Awaitable[Capabilities]
]


def _exact_id(value: int) -> Range:
    """Pin an integer argument to a single value."""
    return Range(value, value)


async def _invoice_policy(
    task_data: dict[str, Any],
    session_context: SessionContext,
    agent_config: dict[str, Any],
) -> Capabilities:
    """Authority for one invoice: read it, read its vendor, and decide it.

    "approved" is only granted when the stored amount is within the agent's
    limit. Over the limit, the agent can still reject the invoice or leave it
    in processing for human review.
    """
    capabilities: Capabilities = [
        ("taxcalc__calculate_tax", {}),
        ("taxcalc__get_tax_rates", {}),
        ("taxcalc__validate_tax_id", {}),
        ("complete_task", {}),
    ]

    invoice_id = task_data.get("invoice_id")
    if invoice_id is None:
        # Informational task with no invoice in scope: no write authority.
        return capabilities

    invoice = await get_invoice_details(int(invoice_id), session_context)
    statuses = ["processing", "rejected"]
    if invoice["amount"] <= agent_config["max_invoice_amount"]:
        statuses.append("approved")

    return capabilities + [
        ("get_invoice_details", {"invoice_id": _exact_id(invoice["id"])}),
        ("get_vendor_details", {"vendor_id": _exact_id(invoice["vendor_id"])}),
        (
            "update_invoice_status",
            {
                "invoice_id": _exact_id(invoice["id"]),
                "status": OneOf(statuses),
                "agent_notes": Wildcard(),
            },
        ),
    ]


async def _payments_policy(
    task_data: dict[str, Any],
    session_context: SessionContext,
    agent_config: dict[str, Any],
) -> Capabilities:
    """Authority to pay one approved invoice to its vendor's account on file.

    Payment authority exists only if the invoice is already "approved" when
    the task starts, and only for that vendor's bank account and at most the
    invoice amount. FinStripe's create_transfer does not check invoice status
    itself, so without this an agent can move money for an unapproved invoice.
    """
    capabilities: Capabilities = [
        ("taxcalc__calculate_tax", {}),
        ("taxcalc__get_tax_rates", {}),
        ("taxcalc__validate_tax_id", {}),
        ("finstripe__get_transfer", {}),
        ("finstripe__get_account_balance", {}),
        ("complete_task", {}),
    ]

    invoice_id = task_data.get("invoice_id")
    if invoice_id is None:
        return capabilities

    invoice = await get_invoice_details(int(invoice_id), session_context)
    vendor = await get_vendor_details(invoice["vendor_id"], session_context)
    invoice_pin = _exact_id(invoice["id"])
    vendor_pin = _exact_id(invoice["vendor_id"])
    capabilities += [
        ("get_invoice_for_payment", {"invoice_id": invoice_pin}),
        ("get_vendor_details", {"vendor_id": vendor_pin}),
        ("get_vendor_payment_summary", {"vendor_id": vendor_pin}),
        ("finstripe__list_transfers", {"vendor_id": vendor_pin, "_allow_unknown": True}),
    ]
    if invoice["status"] != "approved":
        return capabilities

    return capabilities + [
        (
            "process_payment",
            {
                "invoice_id": invoice_pin,
                "payment_method": Wildcard(),
                "payment_reference": Wildcard(),
                "agent_notes": Wildcard(),
            },
        ),
        (
            "finstripe__create_transfer",
            {
                "invoice_id": invoice_pin,
                "vendor_id": vendor_pin,
                "vendor_account": Exact(vendor["bank_account_number"]),
                "amount": Range(0, float(invoice["amount"])),
                # payment_method, currency, description, invoice_reference
                "_allow_unknown": True,
            },
        ),
    ]


POLICIES: dict[str, Policy] = {
    "invoice_agent": _invoice_policy,
    "payments_agent": _payments_policy,
}


class TenuoGuard:
    """Holds one task's warrant and checks tool calls against it."""

    def __init__(self, warrant: Warrant, holder_key: SigningKey):
        self.warrant = warrant
        self._holder_key = holder_key

    @classmethod
    async def for_task(
        cls,
        agent_name: str,
        task_data: dict[str, Any] | None,
        session_context: SessionContext,
        agent_config: dict[str, Any],
    ) -> "TenuoGuard | None":
        """Mint a warrant for this task, or return None if the agent has no policy."""
        policy = POLICIES.get(agent_name)
        if policy is None:
            return None

        capabilities = await policy(task_data or {}, session_context, agent_config)
        holder_key = SigningKey.generate()
        builder = Warrant.mint_builder()
        for tool_name, constraints in capabilities:
            builder = builder.capability(tool_name, **constraints)
        warrant = (
            builder.holder(holder_key.public_key)
            .ttl(WARRANT_TTL_SECONDS)
            .mint(_ISSUER_KEY)
        )
        logger.info(
            "Tenuo warrant minted for %s: tools=%s", agent_name, warrant.tools
        )
        return cls(warrant, holder_key)

    def check(self, tool_name: str, arguments: dict[str, Any]) -> str | None:
        """Return None if the call is authorized, otherwise the denial reason."""
        signature = self.warrant.sign(
            self._holder_key, tool_name, arguments, int(time.time())
        )
        try:
            _AUTHORIZER.authorize(self.warrant, tool_name, arguments, bytes(signature))
        except Exception as e:  # pylint: disable=broad-exception-caught
            return str(e)
        return None
