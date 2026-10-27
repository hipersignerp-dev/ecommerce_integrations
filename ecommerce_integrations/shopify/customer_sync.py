# Copyright (c) 2026, Hipersign ERP and contributors
# For license information, please see LICENSE
"""Shopify customers -> ERPNext in real time (customers/update, customers/delete).

The standard connector creates a customer with their first order and never updates them.
Here later changes to their name, email, phone or default address are copied to the ERPNext
Customer, Contact and Address as they happen.
Customers deleted in Shopify are disabled (they may have invoices). Shopify is the master for
customer details; edits in ERPNext are not sent back.
"""

import frappe
from frappe import _
from frappe.utils import cint, cstr, validate_phone_number

from ecommerce_integrations.shopify.constants import CUSTOMER_ID_FIELD, SETTING_DOCTYPE
from ecommerce_integrations.shopify.utils import create_shopify_log


def sync_customer_webhook(payload, request_id=None):
	"""customers/update."""
	from ecommerce_integrations.shopify.customer import ShopifyCustomer

	frappe.set_user("Administrator")
	frappe.flags.request_id = request_id
	setting = frappe.get_doc(SETTING_DOCTYPE)

	if not cint(setting.get("sync_customers_from_shopify")):
		create_shopify_log(status="Invalid", message=_("Customer sync from Shopify is disabled."))
		return

	try:
		customer = ShopifyCustomer(customer_id=cstr(payload.get("id")))
		if not customer.is_synced():
			# created by the order sync with their first order; creating it here as well would
			# race with orders/create (both fire at checkout) and duplicate the customer
			create_shopify_log(
				status="Invalid",
				message=_("Shopify customer {0} has no orders yet; skipped.").format(payload.get("id")),
			)
			return
		message = update_customer(customer, payload)
	except Exception as e:
		create_shopify_log(status="Error", exception=e, rollback=True)
	else:
		create_shopify_log(status="Success", message=message)


def delete_customer_webhook(payload, request_id=None):
	frappe.set_user("Administrator")
	frappe.flags.request_id = request_id
	try:
		name = frappe.db.get_value("Customer", {CUSTOMER_ID_FIELD: cstr(payload.get("id"))})
		if name:
			frappe.db.set_value("Customer", name, "disabled", 1)
	except Exception as e:
		create_shopify_log(status="Error", exception=e, rollback=True)
	else:
		create_shopify_log(status="Success", message=_("Disabled customer {0}.").format(name or "-"))


def update_customer(customer, payload) -> str:
	doc = customer.get_customer_doc()
	changes = []

	full_name = f"{cstr(payload.get('first_name'))} {cstr(payload.get('last_name'))}".strip()
	full_name = full_name or cstr(payload.get("email"))
	if full_name and doc.customer_name != full_name:
		doc.customer_name = full_name
		doc.flags.ignore_mandatory = True
		doc.save(ignore_permissions=True)
		changes.append("name")

	if _update_contact(doc.name, payload):
		changes.append("contact")

	address = payload.get("default_address")
	if address and _address_changed(customer, address):
		customer._update_existing_address(full_name, address, "Billing", payload.get("email"))
		changes.append("address")

	if not changes:
		return _("No changes for customer {0}.").format(doc.name)
	return _("Updated {0} for customer {1}.").format(", ".join(changes), doc.name)


def _update_contact(customer_name, payload) -> bool:
	email = cstr(payload.get("email")).strip()
	phone = cstr(payload.get("phone") or (payload.get("default_address") or {}).get("phone")).strip()

	contact_name = frappe.db.get_value(
		"Dynamic Link",
		{"link_doctype": "Customer", "link_name": customer_name, "parenttype": "Contact"},
		"parent",
	)
	if not contact_name:
		return False

	contact = frappe.get_doc("Contact", contact_name)
	changed = False

	for field in ("first_name", "last_name"):
		value = cstr(payload.get(field)).strip()
		if value and cstr(contact.get(field)) != value:
			contact.set(field, value)
			changed = True

	if email and email not in [e.email_id for e in contact.email_ids]:
		for row in contact.email_ids:
			row.is_primary = 0
		contact.append("email_ids", {"email_id": email, "is_primary": 1})
		changed = True

	if (
		phone
		and validate_phone_number(phone, throw=False)
		and phone not in [p.phone for p in contact.phone_nos]
	):
		for row in contact.phone_nos:
			row.is_primary_phone = 0
		contact.append("phone_nos", {"phone": phone, "is_primary_phone": 1})
		changed = True

	if changed:
		contact.flags.ignore_mandatory = True
		contact.save(ignore_permissions=True)
	return changed


def _address_changed(customer, shopify_address) -> bool:
	current = customer.get_customer_address_doc("Billing")
	if not current:
		return True

	pairs = {
		"address_line1": shopify_address.get("address1"),
		"address_line2": shopify_address.get("address2"),
		"city": shopify_address.get("city"),
		"state": shopify_address.get("province"),
		"pincode": shopify_address.get("zip"),
		"country": shopify_address.get("country"),
	}
	return any(cstr(current.get(k)).strip() != cstr(v).strip() for k, v in pairs.items())
