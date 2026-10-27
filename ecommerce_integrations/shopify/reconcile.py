# Copyright (c) 2026, Hipersign ERP and contributors
# For license information, please see LICENSE
"""Nightly safety net: make ERPNext match Shopify even if a webhook was missed.

Shopify delivers webhooks within seconds, but delivery is not guaranteed (site down, worker
restart, Shopify retries exhausted). Once a day this job reads every Shopify order updated in
the last few days and replays whatever is missing in ERPNext, through the same handlers the
webhooks use:

- order with no Sales Order          -> sync_sales_order (also invoices / delivers as needed)
- paid order with no Sales Invoice   -> prepare_sales_invoice
- fulfillment with no Delivery Note  -> prepare_delivery_note
- cancelled order still open         -> cancel_order
- refund with no credit note         -> sync_refund

It then re-sends stock for every linked item. A summary is written to Ecommerce Integration Log.
"""

import json

import frappe
from frappe.utils import add_days, cint, cstr, flt, get_datetime, now_datetime
from shopify.collection import PaginatedIterator
from shopify.resources import Order

from ecommerce_integrations.shopify.connection import temp_shopify_session
from ecommerce_integrations.shopify.constants import (
	EVENT_MAPPER,
	ORDER_ID_FIELD,
	ORDER_STATUS_FIELD,
	REFUND_ID_FIELD,
	SETTING_DOCTYPE,
)
from ecommerce_integrations.shopify.utils import create_shopify_log

LOOKBACK_DAYS = 3
PAID_STATUSES = ("paid", "partially_refunded", "refunded")


def run_nightly_reconciliation():
	"""Scheduler entry point (daily_long)."""
	setting = frappe.get_doc(SETTING_DOCTYPE)
	if not setting.is_enabled() or not cint(setting.get("nightly_reconciliation")):
		return

	reconcile_orders(days=cint(setting.get("reconciliation_days")) or LOOKBACK_DAYS)

	from ecommerce_integrations.shopify.inventory_realtime import push_changed_inventory

	push_changed_inventory(force_all=True)


@frappe.whitelist()
def run_now():
	"""Button in Shopify Setting: queue the reconciliation immediately."""
	frappe.only_for("System Manager")
	frappe.enqueue(
		"ecommerce_integrations.shopify.reconcile.run_nightly_reconciliation",
		queue="long",
		timeout=3600,
		job_id="shopify_reconciliation",
		deduplicate=True,
	)


@temp_shopify_session
def reconcile_orders(days=LOOKBACK_DAYS):
	frappe.set_user("Administrator")
	since = get_datetime(add_days(now_datetime(), -days)).astimezone().isoformat()

	stats = {"checked": 0, "fixed": 0}
	fixes = []

	orders = PaginatedIterator(Order.find(updated_at_min=since, status="any", limit=250))
	for page in orders:
		for order in page:
			order = order.to_dict()
			stats["checked"] += 1
			for action in reconcile_order(order):
				stats["fixed"] += 1
				fixes.append(f"{order.get('name')}: {action}")

	frappe.flags.request_id = None
	create_shopify_log(
		status="Success",
		method="ecommerce_integrations.shopify.reconcile.reconcile_orders",
		message=f"Checked {stats['checked']} Shopify orders from the last {days} days, "
		f"replayed {stats['fixed']} missing updates.\n" + "\n".join(fixes),
		make_new=True,
	)


def reconcile_order(order) -> list[str]:
	"""Replay what ERPNext is missing for one Shopify order; returns the actions taken."""
	from ecommerce_integrations.shopify.fulfillment import prepare_delivery_note
	from ecommerce_integrations.shopify.fulfillment_push import is_fulfillment_synced
	from ecommerce_integrations.shopify.invoice import prepare_sales_invoice
	from ecommerce_integrations.shopify.order import cancel_order, sync_sales_order
	from ecommerce_integrations.shopify.refund import sync_refund

	order_id = cstr(order.get("id"))
	actions = []

	sales_order = frappe.db.get_value(
		"Sales Order",
		{ORDER_ID_FIELD: order_id},
		["name", "docstatus", "status", ORDER_STATUS_FIELD],
		as_dict=True,
	)

	if not sales_order:
		if order.get("cancelled_at") and order.get("financial_status") not in PAID_STATUSES:
			return actions  # cancelled before anything happened: nothing to record

		_replay("orders/create", sync_sales_order, order)
		actions.append("created sales order")
		return actions  # sync_sales_order also invoices and delivers

	if sales_order.docstatus == 2:
		return actions

	if order.get("financial_status") in PAID_STATUSES and not frappe.db.exists(
		"Sales Invoice", {ORDER_ID_FIELD: order_id, "docstatus": 1, "is_return": 0}
	):
		_replay("orders/paid", prepare_sales_invoice, order)
		actions.append("created sales invoice")

	missing_fulfillments = [
		f
		for f in order.get("fulfillments") or []
		if f.get("status") == "success" and not is_fulfillment_synced(order_id, f.get("id"))
	]
	if missing_fulfillments:
		_replay("orders/fulfilled", prepare_delivery_note, order)
		actions.append(f"created delivery note for {len(missing_fulfillments)} fulfilment(s)")

	if order.get("cancelled_at") and not cstr(sales_order.get(ORDER_STATUS_FIELD)).startswith("cancelled"):
		_replay("orders/cancelled", cancel_order, order)
		actions.append("applied cancellation")

	for refund in order.get("refunds") or []:
		has_items = any(flt(rli.get("quantity")) for rli in refund.get("refund_line_items") or [])
		if has_items and not frappe.db.exists(
			"Sales Invoice", {REFUND_ID_FIELD: cstr(refund.get("id")), "docstatus": ("<", 2)}
		):
			refund.setdefault("order_id", order.get("id"))
			_replay("refunds/create", sync_refund, refund)
			actions.append(f"synced refund {refund.get('id')}")

	return actions


def _replay(topic, handler, payload):
	"""Run a webhook handler synchronously with its own log, like a delivered webhook."""
	log = create_shopify_log(
		method=EVENT_MAPPER[topic],
		request_data=json.dumps(payload),
		message="Replayed by nightly reconciliation",
		make_new=True,
	)
	handler(payload, request_id=log.name)
	frappe.flags.request_id = None
