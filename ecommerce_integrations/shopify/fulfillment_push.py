# Copyright (c) 2026, Hipersign ERP and contributors
# For license information, please see LICENSE
"""Push Delivery Notes made in ERPNext to Shopify as fulfillments.

When a Delivery Note for a Shopify order is submitted in ERPNext, the shipped quantities are
fulfilled on the order in Shopify, with the Transport Receipt No as tracking number and the
Transporter Name as carrier. Shopify can then email the customer their shipping update.

Echo protection: Shopify answers every fulfillment with an orders/fulfilled webhook, which the
standard connector would turn into a second Delivery Note. Before calling Shopify the DN's
fulfillment id is set to a "pushing" marker, and afterwards to the real fulfillment id(s);
``is_fulfillment_synced`` makes the webhook skip both.
"""

import json
import time
from collections import defaultdict
from contextlib import contextmanager

import frappe
import shopify
from frappe import _
from frappe.utils import cint, cstr, flt
from shopify.session import Session

from ecommerce_integrations.shopify.constants import (
	FULFILLMENT_PUSH_MARKER,
	FULLFILLMENT_ID_FIELD,
	GRAPHQL_API_VERSION,
	ORDER_ID_FIELD,
	SETTING_DOCTYPE,
)
from ecommerce_integrations.shopify.utils import create_shopify_log

PUSH_METHOD = "ecommerce_integrations.shopify.fulfillment_push.push_fulfillment_job"
STALE_MARKER_SECONDS = 15 * 60

FULFILLMENT_ORDERS_QUERY = """
query FulfillmentOrders($id: ID!) {
  order(id: $id) {
    fulfillmentOrders(first: 20) {
      nodes {
        id
        status
        assignedLocation { location { legacyResourceId } }
        lineItems(first: 100) {
          nodes {
            id
            remainingQuantity
            lineItem {
              sku
              product { legacyResourceId }
              variant { legacyResourceId }
            }
          }
        }
      }
    }
  }
}
"""

FULFILLMENT_CREATE_MUTATION = """
mutation FulfillmentCreate($fulfillment: FulfillmentInput!) {
  fulfillmentCreate(fulfillment: $fulfillment) {
    fulfillment { id legacyResourceId status }
    userErrors { field message }
  }
}
"""

OPEN_FULFILLMENT_ORDER_STATUSES = ("OPEN", "IN_PROGRESS")


# --- hook ---


def on_delivery_note_submit(doc, method=None):
	"""doc_events hook: queue the push for Delivery Notes created in ERPNext."""
	if doc.is_return or doc.get(FULLFILLMENT_ID_FIELD):
		# returns, and DNs that came *from* a Shopify fulfillment, are never pushed back
		return

	setting = frappe.get_cached_doc(SETTING_DOCTYPE)
	if not setting.is_enabled() or not cint(setting.get("push_delivery_note_to_shopify")):
		return

	order_id = get_shopify_order_id(doc)
	if not order_id:
		return

	if not doc.get(ORDER_ID_FIELD):
		doc.db_set(ORDER_ID_FIELD, order_id, update_modified=False)

	doc.db_set(FULLFILLMENT_ID_FIELD, f"{FULFILLMENT_PUSH_MARKER}{int(time.time())}", update_modified=False)
	frappe.enqueue(PUSH_METHOD, queue="short", timeout=300, enqueue_after_commit=True, dn_name=doc.name)


def get_shopify_order_id(doc) -> str | None:
	if doc.get(ORDER_ID_FIELD):
		return cstr(doc.get(ORDER_ID_FIELD))

	sales_orders = {item.against_sales_order for item in doc.items if item.against_sales_order}
	for sales_order in sales_orders:
		if order_id := frappe.db.get_value("Sales Order", sales_order, ORDER_ID_FIELD):
			return cstr(order_id)


# --- background job ---


def push_fulfillment_job(dn_name):
	frappe.set_user("Administrator")
	frappe.flags.request_id = None
	dn = frappe.get_doc("Delivery Note", dn_name)

	try:
		fulfillment_ids = push_delivery_note(dn)
	except Exception as e:
		frappe.db.rollback()
		frappe.db.set_value("Delivery Note", dn_name, FULLFILLMENT_ID_FIELD, "", update_modified=False)
		create_shopify_log(
			status="Error",
			method=PUSH_METHOD,
			request_data={"delivery_note": dn_name},
			exception=e,
			make_new=True,
		)
	else:
		frappe.db.set_value(
			"Delivery Note", dn_name, FULLFILLMENT_ID_FIELD, ",".join(fulfillment_ids), update_modified=False
		)
		create_shopify_log(
			status="Success",
			method=PUSH_METHOD,
			request_data={"delivery_note": dn_name},
			message=_("Delivery Note {0} fulfilled in Shopify ({1}).").format(
				dn_name, ", ".join(fulfillment_ids)
			),
			make_new=True,
		)


def push_delivery_note(dn) -> list[str]:
	"""Create Shopify fulfillment(s) for the DN's quantities and return their ids."""
	from ecommerce_integrations.shopify.product import get_item_code

	setting = frappe.get_doc(SETTING_DOCTYPE)
	order_id = get_shopify_order_id(dn)

	to_ship = defaultdict(float)
	for item in dn.items:
		to_ship[item.item_code] += flt(item.stock_qty or item.qty)

	with graphql_session(setting) as client:
		data = run_graphql(client, FULFILLMENT_ORDERS_QUERY, {"id": f"gid://shopify/Order/{order_id}"})
		order = data.get("order")
		if not order:
			frappe.throw(_("Shopify order {0} not found.").format(order_id))

		# fulfillmentCreate only accepts fulfillment orders of one location at a time
		by_location = defaultdict(list)
		for fulfillment_order in order["fulfillmentOrders"]["nodes"]:
			if fulfillment_order["status"] not in OPEN_FULFILLMENT_ORDER_STATUSES:
				continue

			lines = []
			for line in fulfillment_order["lineItems"]["nodes"]:
				remaining = cint(line["remainingQuantity"])
				line_item = line.get("lineItem") or {}
				item_code = get_item_code(
					{
						"product_id": (line_item.get("product") or {}).get("legacyResourceId"),
						"variant_id": (line_item.get("variant") or {}).get("legacyResourceId"),
						"sku": line_item.get("sku"),
					}
				)
				qty = min(remaining, cint(to_ship.get(item_code, 0)))
				if qty <= 0:
					continue
				to_ship[item_code] -= qty
				lines.append({"id": line["id"], "quantity": qty})

			if lines:
				location = (fulfillment_order.get("assignedLocation") or {}).get("location") or {}
				by_location[location.get("legacyResourceId")].append(
					{"fulfillmentOrderId": fulfillment_order["id"], "fulfillmentOrderLineItems": lines}
				)

		if not by_location:
			frappe.throw(_("Nothing left to fulfil in Shopify for order {0}.").format(order_id))

		fulfillment_ids = []
		for line_items_by_fulfillment_order in by_location.values():
			fulfillment = {
				"lineItemsByFulfillmentOrder": line_items_by_fulfillment_order,
				"notifyCustomer": bool(cint(setting.get("notify_customer_on_fulfillment"))),
			}
			tracking = get_tracking_info(dn)
			if tracking:
				fulfillment["trackingInfo"] = tracking

			result = run_graphql(client, FULFILLMENT_CREATE_MUTATION, {"fulfillment": fulfillment})
			payload = result["fulfillmentCreate"]
			if payload.get("userErrors"):
				frappe.throw("; ".join(e["message"] for e in payload["userErrors"]))
			fulfillment_ids.append(cstr(payload["fulfillment"]["legacyResourceId"]))

	not_shipped = {code: qty for code, qty in to_ship.items() if qty > 0}
	if not_shipped:
		dn.add_comment(
			"Comment",
			_("Not fulfilled in Shopify (not open on the order there): {0}").format(
				", ".join(f"{code} x {qty:g}" for code, qty in not_shipped.items())
			),
		)

	return fulfillment_ids


def get_tracking_info(dn) -> dict:
	tracking = {}
	if dn.get("lr_no"):
		tracking["number"] = dn.lr_no
	if dn.get("transporter_name"):
		tracking["company"] = dn.transporter_name
	return tracking


# --- echo protection, used by fulfillment.create_delivery_note ---


def is_fulfillment_synced(order_id, fulfillment_id) -> bool:
	"""True when a DN already stands for this Shopify fulfillment, or a push is in flight."""
	values = frappe.get_all(
		"Delivery Note",
		filters={
			ORDER_ID_FIELD: cstr(order_id),
			"docstatus": ("<", 2),
			FULLFILLMENT_ID_FIELD: ("is", "set"),
		},
		pluck=FULLFILLMENT_ID_FIELD,
	)
	values += frappe.get_all(
		"Delivery Note", filters={FULLFILLMENT_ID_FIELD: cstr(fulfillment_id)}, pluck=FULLFILLMENT_ID_FIELD
	)

	for value in values:
		value = cstr(value)
		if value.startswith(FULFILLMENT_PUSH_MARKER):
			started = cint(value[len(FULFILLMENT_PUSH_MARKER) :])
			if time.time() - started < STALE_MARKER_SECONDS:
				return True
		elif cstr(fulfillment_id) in value.split(","):
			return True
	return False


# --- GraphQL helpers ---


@contextmanager
def graphql_session(setting):
	from ecommerce_integrations.shopify.connection import _get_access_token

	with Session.temp(setting.shopify_url, GRAPHQL_API_VERSION, _get_access_token(setting)):
		yield shopify.GraphQL()


def run_graphql(client, query, variables=None) -> dict:
	response = json.loads(client.execute(query, variables=variables))
	if response.get("errors"):
		frappe.throw(_("Shopify GraphQL error: {0}").format(json.dumps(response["errors"])))
	return response.get("data") or {}
