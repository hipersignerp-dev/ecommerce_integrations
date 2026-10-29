# Copyright (c) 2026, Hipersign ERP and contributors
# For license information, please see LICENSE
"""Push stock to Shopify as soon as it changes in ERPNext.

The standard connector pushes stock on a timer (5-60 minutes). With "Instant Stock Sync" on,
every stock posting (Stock Ledger Entry) and every change in Sales Order reservations queues a
push that runs right after the transaction commits. Pushes are de-duplicated, so a document
that posts fifty ledger entries still makes a single push, and the push itself only sends the
items whose stock changed since their last sync (same delta logic as the scheduled job).
"""

import frappe
from frappe.utils import cint

from ecommerce_integrations.controllers.inventory import get_inventory_levels
from ecommerce_integrations.shopify.constants import MODULE_NAME, SETTING_DOCTYPE

PUSH_JOB_ID = "shopify_instant_inventory_push"
PUSH_METHOD = "ecommerce_integrations.shopify.inventory_realtime.push_changed_inventory"


def queue_inventory_push(doc=None, method=None):
	"""doc_events hook for Stock Ledger Entry / Sales Order changes."""
	if frappe.flags.in_install or frappe.flags.in_migrate or frappe.flags.in_test:
		return

	if not _instant_sync_enabled():
		return

	frappe.enqueue(
		PUSH_METHOD,
		queue="short",
		timeout=600,
		job_id=PUSH_JOB_ID,
		deduplicate=True,
		enqueue_after_commit=True,
	)


def queue_inventory_push_if_submitted(doc, method=None):
	"""Sales Order on_change: status changes (Close, Re-open, Hold) release or re-reserve stock."""
	if doc.docstatus == 1:
		queue_inventory_push(doc, method)


def push_changed_inventory(force_all: bool = False):
	"""Push every Shopify-linked item whose stock changed since its last sync.

	force_all re-sends all linked items (used by the nightly reconciliation).
	"""
	from ecommerce_integrations.shopify.inventory import upload_inventory_data_to_shopify

	setting = frappe.get_doc(SETTING_DOCTYPE)
	if not setting.is_enabled() or not cint(setting.update_erpnext_stock_levels_to_shopify):
		return

	warehouse_map = setting.get_erpnext_to_integration_wh_mapping()
	if not warehouse_map:
		return

	if force_all:
		frappe.db.set_value(
			"Ecommerce Item",
			{"integration": MODULE_NAME},
			"inventory_synced_on",
			"1970-01-01 00:00:00",
			update_modified=False,
		)

	inventory_levels = get_inventory_levels(tuple(warehouse_map.keys()), MODULE_NAME)
	if inventory_levels:
		upload_inventory_data_to_shopify(inventory_levels, warehouse_map)


def _instant_sync_enabled() -> bool:
	values = frappe.db.get_value(
		SETTING_DOCTYPE,
		SETTING_DOCTYPE,
		["enable_shopify", "update_erpnext_stock_levels_to_shopify", "instant_inventory_sync"],
		as_dict=True,
		cache=True,
	)
	return bool(
		values
		and cint(values.enable_shopify)
		and cint(values.update_erpnext_stock_levels_to_shopify)
		and cint(values.instant_inventory_sync)
	)
