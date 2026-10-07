"""What each FinBot agent may do for the task in front of it.

A policy turns trusted facts (see facts.py) into a spec: {tool: {arg: constraint}}.
Every argument a tool takes is listed, using Wildcard where the policy doesn't
care, so a narrower warrant always has the same argument set as its parent.

The policies encode FinBot's own business rules (approval limits, who gets
paid, who can be emailed) as authority, so a manipulated agent can't act
outside them no matter what it is told.
"""

from typing import Any, Callable

from tenuo import AnyOf, OneOf, Range, Subset, Wildcard

from finbot.tenuo.facts import Facts

Spec = dict[str, dict[str, Any]]

# Argument names for every tool an agent can call (native and MCP).
TOOL_ARGS: dict[str, list[str]] = {
    # Control flow
    "complete_task": ["task_status", "task_summary"],
    # Orchestrator
    "delegate_to_onboarding": ["vendor_id", "task_description"],
    "delegate_to_invoice": ["invoice_id", "task_description"],
    "delegate_to_fraud": ["vendor_id", "task_description"],
    "delegate_to_payments": ["invoice_id", "task_description"],
    "delegate_to_system_maintenance": ["vendor_id", "task_description"],
    "delegate_to_communication": ["vendor_id", "task_description", "notification_type",
                                  "to_addresses", "cc_addresses", "bcc_addresses"],
    # Native data tools
    "get_vendor_details": ["vendor_id"],
    "get_vendor_contact_info": ["vendor_id"],
    "get_vendor_invoices": ["vendor_id"],
    "get_vendor_payment_summary": ["vendor_id"],
    "get_vendor_risk_profile": ["vendor_id"],
    "get_vendor_compliance_docs": ["vendor_id"],
    "get_vendor_activity_report": ["vendor_id"],
    "get_invoice_details": ["invoice_id"],
    "get_invoice_for_payment": ["invoice_id"],
    "update_invoice_status": ["invoice_id", "status", "agent_notes"],
    "update_vendor_status": ["vendor_id", "status", "agent_notes", "trust_level", "risk_level"],
    "update_vendor_risk": ["vendor_id", "risk_level", "agent_notes"],
    "flag_invoice_for_review": ["invoice_id", "flag_reason", "recommended_action", "agent_notes"],
    "process_payment": ["invoice_id", "payment_method", "payment_reference", "agent_notes"],
    "list_vendors": [],
    "get_all_vendors_summary": [],
    "get_pending_actions_summary": [],
    "save_report": ["title", "content", "report_type"],
    "start_workflow": ["description", "vendor_id", "invoice_id", "attachment_file_ids"],
    # MCP: FinDrive
    "findrive__upload_file": ["filename", "content", "folder", "vendor_id", "file_type"],
    "findrive__get_file": ["file_id"],
    "findrive__list_files": ["folder", "vendor_id", "limit"],
    "findrive__delete_file": ["file_id"],
    "findrive__search_files": ["query", "limit"],
    # MCP: FinMail
    "finmail__send_email": ["to", "subject", "body", "message_type", "sender_name",
                            "cc", "bcc", "related_invoice_id"],
    "finmail__list_inbox": ["inbox", "vendor_id", "message_type", "unread_only", "limit"],
    "finmail__read_email": ["message_id"],
    "finmail__search_emails": ["query", "inbox", "vendor_id", "limit"],
    "finmail__mark_as_read": ["message_id"],
    # MCP: SystemUtils
    "systemutils__run_diagnostics": ["command"],
    # MCP: FinStripe
    "finstripe__create_transfer": ["vendor_account", "amount", "invoice_reference", "vendor_id",
                                   "invoice_id", "payment_method", "currency", "description"],
    "finstripe__get_transfer": ["transfer_id"],
    "finstripe__get_account_balance": ["account_id"],
    "finstripe__list_transfers": ["vendor_id", "limit"],
    # MCP: TaxCalc
    "taxcalc__calculate_tax": ["amount", "jurisdiction", "category"],
    "taxcalc__get_tax_rates": ["jurisdiction"],
    "taxcalc__validate_tax_id": ["tax_id", "country"],
}

TAX_TOOLS = ["taxcalc__calculate_tax", "taxcalc__get_tax_rates", "taxcalc__validate_tax_id"]
MAIL_READ_TOOLS = ["finmail__list_inbox", "finmail__read_email",
                   "finmail__search_emails", "finmail__mark_as_read"]
SAFE_DIAGNOSTICS = ["disk_usage", "memory_check", "network_status", "process_list"]


def pin(value: int) -> Range:
    """Pin an integer argument to one value."""
    return Range(value, value)


def any_id(ids: list[int]):
    """Allow any of a set of integer IDs, or None if the set is empty."""
    ids = sorted(set(ids))
    if not ids:
        return None
    if len(ids) == 1:
        return pin(ids[0])
    return AnyOf([pin(i) for i in ids])


def spec(tools: dict[str, dict[str, Any]]) -> Spec:
    """Fill in Wildcard for every argument a tool takes but the policy leaves open.

    Tools whose ID constraint resolved to None (e.g. no files to read) are dropped.
    """
    out: Spec = {}
    for tool, constraints in tools.items():
        if any(c is None for c in constraints.values()):
            continue
        args = TOOL_ARGS.get(tool)
        if args is None:
            out[tool] = dict(constraints)
            continue
        out[tool] = {arg: constraints.get(arg, Wildcard()) for arg in args}
    return out


def _vendor_reads(vendor_id: int, *tools: str) -> dict[str, dict[str, Any]]:
    return {t: {"vendor_id": pin(vendor_id)} for t in tools}


def _recipients(f: Facts, *, all_vendors: bool = False) -> list[str]:
    allowed = list(f.internal_addresses)
    if all_vendors:
        allowed += f.all_vendor_emails
    elif f.vendor and f.vendor.get("email"):
        allowed.append(f.vendor["email"])
    return sorted(set(allowed))


# --- Specialized agents --------------------------------------------------------


def invoice_policy(f: Facts, cfg: dict, **_) -> Spec:
    """Read this invoice and its vendor; decide only this invoice.

    "approved" is granted only within FinBot's rules: the amount is within the
    limit, and a low-trust vendor stays under the manual-review threshold.
    """
    tools: dict[str, dict[str, Any]] = {t: {} for t in TAX_TOOLS}
    tools["complete_task"] = {}
    inv, vendor = f.invoice, f.vendor
    if not inv or not vendor:
        return spec(tools)

    statuses = ["processing", "rejected"]
    within_limit = inv["amount"] <= cfg.get("max_invoice_amount", 50000)
    needs_review = (
        vendor["trust_level"] == "low"
        and inv["amount"] >= cfg.get("manual_review_threshold", 10000)
    )
    if within_limit and not needs_review:
        statuses.append("approved")

    tools.update({
        "get_invoice_details": {"invoice_id": pin(inv["id"])},
        "get_vendor_details": {"vendor_id": pin(vendor["id"])},
        "update_invoice_status": {"invoice_id": pin(inv["id"]), "status": OneOf(statuses)},
        "findrive__get_file": {"file_id": any_id(f.attachment_file_ids)},
    })
    return spec(tools)


def payments_policy(f: Facts, cfg: dict, *, ceiling: bool = False, **_) -> Spec:
    """Pay one approved invoice, to its vendor's account on file, at most its amount.

    With ceiling=True (the workflow root), payment authority is included as if
    the invoice were approved; the payments agent's own warrant only gets it if
    the invoice is approved when its task starts.
    """
    tools: dict[str, dict[str, Any]] = {t: {} for t in TAX_TOOLS}
    tools.update({"complete_task": {}, "finstripe__get_transfer": {},
                  "finstripe__get_account_balance": {}})
    inv, vendor = f.invoice, f.vendor
    if not inv or not vendor:
        return spec(tools)

    tools.update({
        "get_invoice_for_payment": {"invoice_id": pin(inv["id"])},
        **_vendor_reads(vendor["id"], "get_vendor_details", "get_vendor_payment_summary",
                        "finstripe__list_transfers"),
    })
    if ceiling or inv["status"] == "approved":
        tools["process_payment"] = {"invoice_id": pin(inv["id"]),
                                    "payment_method": OneOf(["bank_transfer", "wire", "ach"])}
        tools["finstripe__create_transfer"] = {
            "invoice_id": pin(inv["id"]),
            "vendor_id": pin(vendor["id"]),
            "vendor_account": OneOf([vendor["bank_account_number"]]),
            "amount": Range(0, float(inv["amount"])),
        }
    return spec(tools)


def fraud_policy(f: Facts, cfg: dict, *, maintenance: bool = False, **_) -> Spec:
    """Review one vendor: read its records and files, flag and rate risk.

    No email, no network, no deletion, no scripts. Maintenance tasks, which
    only an admin can start, additionally get the safe diagnostics.
    """
    tools: dict[str, dict[str, Any]] = {t: {} for t in MAIL_READ_TOOLS}
    tools["complete_task"] = {}
    tools["findrive__search_files"] = {}
    vendor = f.vendor
    if vendor:
        invoices = any_id(f.vendor_invoice_ids)
        tools.update({
            **_vendor_reads(vendor["id"], "get_vendor_risk_profile", "get_vendor_invoices",
                            "findrive__list_files"),
            "get_invoice_details": {"invoice_id": invoices},
            "update_vendor_risk": {"vendor_id": pin(vendor["id"]),
                                   "risk_level": OneOf(["low", "medium", "high"])},
            "flag_invoice_for_review": {"invoice_id": invoices,
                                        "recommended_action": OneOf(["hold", "reject", "escalate"])},
            "findrive__get_file": {"file_id": any_id(f.vendor_file_ids)},
        })
    if maintenance:
        tools["systemutils__run_diagnostics"] = {"command": OneOf(SAFE_DIAGNOSTICS)}
    return spec(tools)


def communication_policy(f: Facts, cfg: dict, **_) -> Spec:
    """Notify this vendor or internal departments, and nobody else."""
    tools: dict[str, dict[str, Any]] = {t: {} for t in MAIL_READ_TOOLS}
    tools["complete_task"] = {}
    vendor = f.vendor
    if vendor:
        allowed = _recipients(f)
        tools.update({
            **_vendor_reads(vendor["id"], "get_vendor_contact_info", "get_vendor_details"),
            "get_invoice_details": {"invoice_id": any_id(f.vendor_invoice_ids)},
            "finmail__send_email": {"to": Subset(allowed), "cc": Subset(allowed),
                                    "bcc": Subset(allowed)},
        })
    return spec(tools)


def onboarding_policy(f: Facts, cfg: dict, **_) -> Spec:
    """Decide this vendor's onboarding within FinBot's rules.

    A rejected (inactive) vendor can go back to review but not straight to
    active. Vendors in the catch-all category or industry can't be given top
    trust or lowest risk automatically.
    """
    tools: dict[str, dict[str, Any]] = {"complete_task": {}}
    vendor = f.vendor
    if not vendor:
        return spec(tools)
    statuses = (["inactive", "pending"] if vendor["status"] == "inactive"
                else ["pending", "active", "inactive"])
    unclassified = (vendor["vendor_category"] == "other_specialized"
                    or vendor["industry"] == "other")
    trust = ["low", "standard"] if unclassified else ["low", "standard", "high"]
    risk = ["medium", "high"] if unclassified else ["low", "medium", "high"]
    tools.update({
        "get_vendor_details": {"vendor_id": pin(vendor["id"])},
        "update_vendor_status": {"vendor_id": pin(vendor["id"]), "status": OneOf(statuses),
                                 "trust_level": OneOf(trust), "risk_level": OneOf(risk)},
    })
    return spec(tools)


def orchestrator_policy(f: Facts, cfg: dict, **_) -> Spec:
    """Delegate only about this workflow's vendor and invoice."""
    tools: dict[str, dict[str, Any]] = {"complete_task": {}}
    vendor, inv = f.vendor, f.invoice
    if vendor:
        tools.update({
            "delegate_to_onboarding": {"vendor_id": pin(vendor["id"])},
            "delegate_to_fraud": {"vendor_id": pin(vendor["id"])},
            "delegate_to_communication": {
                "vendor_id": pin(vendor["id"]),
                "to_addresses": Subset(_recipients(f)),
                "cc_addresses": Subset(_recipients(f)),
                "bcc_addresses": Subset(_recipients(f)),
            },
        })
        if f.is_admin:
            tools["delegate_to_system_maintenance"] = {"vendor_id": pin(vendor["id"])}
    if inv:
        tools["delegate_to_invoice"] = {"invoice_id": pin(inv["id"])}
        tools["delegate_to_payments"] = {"invoice_id": pin(inv["id"])}
    return spec(tools)


# --- Chat assistants ------------------------------------------------------------


def vendor_chat_policy(f: Facts, cfg: dict, **_) -> Spec:
    """The vendor portal assistant: this vendor's records and files only."""
    tools: dict[str, dict[str, Any]] = {t: {} for t in MAIL_READ_TOOLS}
    vendor = f.vendor
    if not vendor:
        return spec(tools)
    own_files = any_id(f.vendor_file_ids)
    allowed = _recipients(f)
    tools.update({
        **_vendor_reads(vendor["id"], "get_vendor_details", "get_vendor_invoices",
                        "get_vendor_payment_summary", "get_vendor_contact_info",
                        "findrive__list_files", "findrive__upload_file"),
        "get_invoice_details": {"invoice_id": any_id(f.vendor_invoice_ids)},
        "start_workflow": {"vendor_id": pin(vendor["id"])},
        "findrive__get_file": {"file_id": own_files},
        "findrive__delete_file": {"file_id": own_files},
        "finmail__send_email": {"to": Subset(allowed), "cc": Subset(allowed), "bcc": Subset(allowed)},
    })
    return spec(tools)


def copilot_policy(f: Facts, cfg: dict, **_) -> Spec:
    """The admin co-pilot: read everything, report, start workflows.

    No deletion, no network requests or scripts, email only to internal
    departments and registered vendors.
    """
    allowed = _recipients(f, all_vendors=True)
    tools: dict[str, dict[str, Any]] = {t: {} for t in [
        "list_vendors", "get_vendor_details", "get_invoice_details", "get_vendor_invoices",
        "get_vendor_payment_summary", "get_vendor_contact_info", "get_all_vendors_summary",
        "get_pending_actions_summary", "get_vendor_compliance_docs",
        "get_vendor_activity_report", "save_report", "start_workflow",
        "findrive__list_files", "findrive__get_file", "findrive__search_files",
        "findrive__upload_file", *MAIL_READ_TOOLS,
    ]}
    tools["finmail__send_email"] = {"to": Subset(allowed), "cc": Subset(allowed), "bcc": Subset(allowed)}
    tools["systemutils__run_diagnostics"] = {"command": OneOf(SAFE_DIAGNOSTICS)}
    return spec(tools)


Policy = Callable[..., Spec]

AGENT_POLICIES: dict[str, Policy] = {
    "orchestrator_agent": orchestrator_policy,
    "invoice_agent": invoice_policy,
    "payments_agent": payments_policy,
    "fraud_agent": fraud_policy,
    "communication_agent": communication_policy,
    "onboarding_agent": onboarding_policy,
}

CHAT_POLICIES: dict[str, Policy] = {
    "chat_assistant": vendor_chat_policy,
    "copilot_assistant": copilot_policy,
}

# Specialized agents the orchestrator can delegate to.
DELEGATES = ["onboarding_agent", "invoice_agent", "fraud_agent",
             "payments_agent", "communication_agent"]


def merge(specs: list[Spec]) -> Spec:
    """Combine specs into one that covers all of them (for a workflow root).

    Where two specs constrain the same argument differently, the merged spec
    uses Wildcard there; each agent's own warrant narrows it again.
    """
    out: Spec = {}
    for s in specs:
        for tool, constraints in s.items():
            if tool not in out:
                out[tool] = dict(constraints)
                continue
            for arg, c in constraints.items():
                if repr(out[tool].get(arg)) != repr(c):
                    out[tool][arg] = Wildcard()
    return out
