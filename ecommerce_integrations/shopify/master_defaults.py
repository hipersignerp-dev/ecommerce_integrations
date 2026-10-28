# Copyright (c) 2026, Hipersign ERP and contributors
# For license information, please see LICENSE
"""Fill site-specific mandatory fields on masters the Shopify sync creates.

Multi-company sites often add a mandatory company link on Customer, Supplier, Item and Item
Group, and India Compliance requires an HSN code on Items. The connector knows neither, so
records it creates fail validation. These ``before_insert`` hooks fill them in, but only for
records the Shopify sync is creating:

- Customer: has a Shopify customer id
- Supplier: has a Shopify supplier id (product vendor)
- Item, Item Group: created while ShopifyProduct.sync_product is running

Configured in Shopify Setting: "Company Field on Masters" (e.g. ``custom_company``) and
"Default HSN Code". Leave the company field empty to turn the company defaulting off.
"""

import frappe
from frappe.utils import cstr

from ecommerce_integrations.shopify.constants import (
	CUSTOMER_ID_FIELD,
	SETTING_DOCTYPE,
	SUPPLIER_ID_FIELD,
)

HSN_FIELD = "gst_hsn_code"


def before_insert(doc, method=None):
	if not _created_by_shopify_sync(doc):
		return

	setting = frappe.get_cached_doc(SETTING_DOCTYPE)
	if not setting.is_enabled():
		return

	_set_company(doc, setting)

	if doc.doctype == "Item":
		_set_item_defaults(doc, setting)
		_set_hsn_code(doc, setting)


def _created_by_shopify_sync(doc) -> bool:
	if doc.doctype == "Customer":
		return bool(doc.get(CUSTOMER_ID_FIELD))
	if doc.doctype == "Supplier":
		return bool(doc.get(SUPPLIER_ID_FIELD))
	# set by ShopifyProduct.sync_product (order sync and the Import Products page)
	return bool(frappe.flags.in_shopify_product_sync)


def _set_company(doc, setting):
	fieldname = cstr(setting.get("company_field_on_masters")).strip()
	if fieldname and doc.meta.has_field(fieldname) and not doc.get(fieldname):
		doc.set(fieldname, setting.company)


def _set_item_defaults(doc, setting):
	"""The connector adds item defaults for the site's default company; use the Shopify one."""
	for row in doc.get("item_defaults") or []:
		if row.company != setting.company:
			row.company = setting.company
			row.default_warehouse = setting.warehouse


def _set_hsn_code(doc, setting):
	if not doc.meta.has_field(HSN_FIELD) or doc.get(HSN_FIELD):
		return

	hsn = None
	if doc.item_group and frappe.get_meta("Item Group").has_field(HSN_FIELD):
		hsn = frappe.db.get_value("Item Group", doc.item_group, HSN_FIELD)

	hsn = hsn or cstr(setting.get("default_hsn_code")).strip()
	if hsn:
		doc.set(HSN_FIELD, hsn)


def belongs_to_shopify_company(item, setting=None) -> bool:
	"""On multi-company sites, only items of the Shopify company may be pushed to Shopify.

	``item`` is an Item doc or item code. Without a "Company Field on Masters" every item counts.
	"""
	setting = setting or frappe.get_cached_doc(SETTING_DOCTYPE)
	fieldname = cstr(setting.get("company_field_on_masters")).strip()
	if not fieldname or not frappe.get_meta("Item").has_field(fieldname):
		return True

	if isinstance(item, str):
		company = frappe.db.get_value("Item", item, fieldname)
	else:
		company = item.get(fieldname)
		if not company and item.get("variant_of"):
			company = frappe.db.get_value("Item", item.variant_of, fieldname)
	return company == setting.company
