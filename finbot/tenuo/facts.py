"""Trusted facts a warrant is built from.

Everything here comes from FinBot's database, keyed by IDs from trusted code
(a route's task_data or the session), never from model output.
"""

from dataclasses import dataclass, field
from typing import Any

from finbot.core.auth.session import SessionContext
from finbot.core.data.database import db_session
from finbot.core.data.models import Invoice, Vendor
from finbot.mcp.servers.finmail.routing import DEPARTMENT_DIRECTORY


@dataclass
class Facts:
    """What the policy knows about the task in front of an agent."""

    namespace: str
    is_admin: bool = False
    vendor: dict[str, Any] | None = None
    invoice: dict[str, Any] | None = None
    vendor_invoice_ids: list[int] = field(default_factory=list)
    vendor_file_ids: list[int] = field(default_factory=list)
    all_vendor_emails: list[str] = field(default_factory=list)

    @property
    def internal_addresses(self) -> list[str]:
        return [f"{dept}@{self.namespace}.finbot" for dept in DEPARTMENT_DIRECTORY]

    @property
    def attachment_file_ids(self) -> list[int]:
        if not self.invoice:
            return []
        ids = []
        for att in self.invoice.get("attachments") or []:
            file_id = att.get("file_id") if isinstance(att, dict) else att
            if isinstance(file_id, int) or (isinstance(file_id, str) and file_id.isdigit()):
                ids.append(int(file_id))
        return ids


def _vendor_dict(vendor: Vendor) -> dict[str, Any]:
    return {
        "id": vendor.id,
        "email": vendor.email,
        "status": vendor.status,
        "trust_level": vendor.trust_level,
        "risk_level": vendor.risk_level,
        "vendor_category": vendor.vendor_category,
        "industry": vendor.industry,
        "bank_account_number": vendor.bank_account_number,
    }


def load_facts(
    session_context: SessionContext,
    vendor_id: int | None = None,
    invoice_id: int | None = None,
) -> Facts:
    """Read the facts for a vendor and/or invoice in the session's namespace.

    An invoice that belongs to a different vendor than `vendor_id` is ignored,
    so a task can't borrow another vendor's invoice.
    """
    # pylint: disable=import-outside-toplevel
    from finbot.mcp.servers.findrive.models import FinDriveFile

    ns = session_context.namespace
    facts = Facts(namespace=ns, is_admin=session_context.portal_type == "admin")
    with db_session() as db:
        invoice = None
        if invoice_id is not None:
            invoice = (
                db.query(Invoice)
                .filter(Invoice.namespace == ns, Invoice.id == int(invoice_id))
                .first()
            )
            if invoice and vendor_id is not None and invoice.vendor_id != int(vendor_id):
                invoice = None
            if invoice and vendor_id is None:
                vendor_id = invoice.vendor_id
        if invoice:
            facts.invoice = invoice.to_dict()

        if vendor_id is not None:
            vendor = (
                db.query(Vendor)
                .filter(Vendor.namespace == ns, Vendor.id == int(vendor_id))
                .first()
            )
            if vendor:
                facts.vendor = _vendor_dict(vendor)
                facts.vendor_invoice_ids = [
                    i.id
                    for i in db.query(Invoice.id).filter(
                        Invoice.namespace == ns, Invoice.vendor_id == vendor.id
                    )
                ]
                facts.vendor_file_ids = [
                    f.id
                    for f in db.query(FinDriveFile.id).filter(
                        FinDriveFile.namespace == ns,
                        FinDriveFile.vendor_id == vendor.id,
                    )
                ]

        facts.all_vendor_emails = [
            v.email for v in db.query(Vendor.email).filter(Vendor.namespace == ns)
        ]
    return facts
