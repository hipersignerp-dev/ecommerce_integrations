# Copyright (c) 2026, Hipersign ERP and contributors
# For license information, please see LICENSE
"""Shopify refunds -> ERPNext credit notes, stock returns and refund payments.

Handles the ``refunds/create`` webhook. For every Shopify refund that refunds line items:

1. A credit note (Sales Invoice return) is made against the order's invoice for the
   refunded quantities, with the refunded line taxes and any refunded shipping.
2. Items Shopify restocked as "return" are brought back with a Delivery Note return,
   but only when the order had actually been delivered in ERPNext.
3. When the refunded amount matches the credit note, a refund Payment Entry is made
   from the Shopify cash/bank account.

Every document carries the Shopify refund id, so a webhook delivered twice is ignored.
Refunds without line items (a goodwill amount, a shipping-only refund) can't be mapped to
items safely and are logged for an accountant instead.
"""

from collections import defaultdict

import frappe
from frappe import _
from frappe.utils import cint, cstr, flt, getdate, nowdate

from ecommerce_integrations.shopify.constants import (
	FULLFILLMENT_ID_FIELD,
	ORDER_ID_FIELD,
	ORDER_NUMBER_FIELD,
	REFUND_ID_FIELD,
	SETTING_DOCTYPE,
)
from ecommerce_integrations.shopify.order import get_tax_account_description, get_tax_account_head
from ecommerce_integrations.shopify.product import get_item_code
from ecommerce_integrations.shopify.utils import create_shopify_log
from ecommerce_integrations.utils.taxation import ITEM_WISE_TAX_KEY, set_item_wise_tax_details

# restock_type values for which Shopify put the goods back into stock after delivery
RESTOCK_RETURN_TYPES = ("return", "legacy_restock")
AMOUNT_TOLERANCE = 0.05


def sync_refund(payload, request_id=None):
	"""Called by the refunds/create webhook."""
	frappe.set_user("Administrator")
	frappe.flags.request_id = request_id

	refund = payload
	setting = frappe.get_doc(SETTING_DOCTYPE)

	try:
		if not cint(setting.get("sync_refunds")):
			create_shopify_log(status="Invalid", message=_("Refund sync is disabled in Shopify Setting."))
			return

		message = process_refund(refund, setting)
	except Exception as e:
		create_shopify_log(status="Error", exception=e, rollback=True)
	else:
		create_shopify_log(status="Success", message=message)


def process_refund(refund, setting) -> str:
	"""Create the ERPNext documents for one Shopify refund and return a summary."""
	refund_id = cstr(refund.get("id"))
	order_id = cstr(refund.get("order_id"))
	refunded_amount = get_refunded_amount(refund)

	if frappe.db.exists("Sales Invoice", {REFUND_ID_FIELD: refund_id, "docstatus": ("<", 2)}):
		return _("Refund {0} is already synced.").format(refund_id)

	line_items = [rli for rli in refund.get("refund_line_items") or [] if flt(rli.get("quantity")) > 0]
	if not line_items:
		frappe.throw(
			_(
				"Shopify refund {0} of {1} has no refunded items (amount-only or shipping-only refund). "
				"Create the credit note manually."
			).format(refund_id, refunded_amount)
		)

	invoice = frappe.db.get_value(
		"Sales Invoice",
		{ORDER_ID_FIELD: order_id, "docstatus": 1, "is_return": 0},
		"name",
		order_by="creation asc",
	)
	if not invoice:
		frappe.throw(
			_("No submitted Sales Invoice found for Shopify order {0}; nothing to credit.").format(order_id)
		)

	summary = []

	credit_note = make_credit_note(invoice, refund, line_items, setting)
	summary.append(_("Credit note {0} for {1}.").format(credit_note.name, abs(credit_note.grand_total)))

	payment_note = make_refund_payment(credit_note, refunded_amount, setting, refund)
	summary.append(payment_note)

	stock_note = make_stock_return(order_id, refund, line_items, setting)
	if stock_note:
		summary.append(stock_note)

	return " ".join(s for s in summary if s)


def get_refunded_amount(refund) -> float:
	"""Money actually sent back to the customer by this refund."""
	return flt(
		sum(
			flt(t.get("amount"))
			for t in refund.get("transactions") or []
			if t.get("kind") == "refund" and t.get("status") == "success"
		),
		2,
	)


# --- credit note ---


def make_credit_note(invoice, refund, line_items, setting):
	from erpnext.accounts.doctype.sales_invoice.sales_invoice import make_sales_return

	refund_id = cstr(refund.get("id"))
	posting_date = getdate(refund.get("created_at")) or nowdate()

	to_credit = _refunded_qty_by_item(line_items)

	shipping_amount, shipping_taxes = get_shipping_refund(refund)
	shipping_item = setting.shipping_item if cint(setting.add_shipping_as_item) else None
	if shipping_item and shipping_amount:
		to_credit[shipping_item] += 1

	credit_note = make_sales_return(invoice)

	items = []
	for row in credit_note.items:
		pending = to_credit.get(row.item_code, 0)
		if pending <= 0:
			continue

		# the mapped row already holds the quantity not yet returned (negative)
		qty = min(pending, abs(flt(row.qty)))
		if qty <= 0:
			continue

		row.qty = -qty
		row.stock_qty = -qty * flt(row.conversion_factor or 1)
		if row.item_code == shipping_item:
			row.rate = flt(shipping_amount)
		to_credit[row.item_code] -= qty
		items.append(row)

	not_found = {code: qty for code, qty in to_credit.items() if qty > 0}
	if not_found:
		frappe.throw(
			_("These refunded items are not on invoice {0} (or are already returned): {1}").format(
				invoice, ", ".join(f"{code} x {qty:g}" for code, qty in not_found.items())
			)
		)

	credit_note.set("items", items)
	credit_note.update_stock = 0
	credit_note.set(REFUND_ID_FIELD, refund_id)
	credit_note.set(ORDER_ID_FIELD, cstr(refund.get("order_id")))
	credit_note.naming_series = setting.sales_invoice_series or credit_note.naming_series
	credit_note.set_posting_time = 1
	credit_note.posting_date = posting_date
	credit_note.due_date = posting_date

	taxes = get_refund_line_taxes(line_items, setting)
	if not shipping_item:
		taxes.extend(get_shipping_refund_rows(shipping_amount, setting))
	taxes.extend(_shipping_tax_rows(shipping_taxes, setting))
	credit_note.set("taxes", taxes)
	set_item_wise_tax_details(credit_note)

	if refund.get("note"):
		credit_note.remarks = _("Shopify refund note: {0}").format(refund.get("note"))

	credit_note.flags.ignore_mandatory = True
	credit_note.insert(ignore_permissions=True)
	credit_note.submit()

	return credit_note


def _refunded_qty_by_item(line_items) -> defaultdict:
	qty_by_item = defaultdict(float)
	for rli in line_items:
		line_item = rli.get("line_item") or {}
		item_code = get_item_code(line_item)
		if not item_code:
			frappe.throw(
				_("Item for refunded Shopify line {0} ({1}) is not synced to ERPNext.").format(
					rli.get("line_item_id"), line_item.get("title") or line_item.get("sku")
				)
			)
		qty_by_item[item_code] += flt(rli.get("quantity"))
	return qty_by_item


def get_refund_line_taxes(line_items, setting) -> list[dict]:
	"""Negative 'Actual' tax rows for the refunded part of each line, like the order's rows."""
	taxes = []
	for rli in line_items:
		line_item = rli.get("line_item") or {}
		item_code = get_item_code(line_item)
		ratio = flt(rli.get("quantity")) / (cint(line_item.get("quantity")) or 1)

		rows = []
		for tax in line_item.get("tax_lines") or []:
			amount = flt(flt(tax.get("price")) * ratio, 2)
			if not amount:
				continue
			rows.append(_tax_row(tax, -amount, setting, item_code))

		# Shopify reports the exact refunded tax; absorb rounding into the last row
		if rows and rli.get("total_tax") is not None:
			diff = flt(-flt(rli.get("total_tax")) - sum(r["tax_amount"] for r in rows), 2)
			if diff:
				rows[-1]["tax_amount"] = flt(rows[-1]["tax_amount"] + diff, 2)
				rate = rows[-1][ITEM_WISE_TAX_KEY][item_code][0]
				rows[-1][ITEM_WISE_TAX_KEY] = {item_code: [rate, rows[-1]["tax_amount"]]}

		taxes.extend(rows)
	return taxes


def _tax_row(tax, amount, setting, item_code=None) -> dict:
	row = {
		"charge_type": "Actual",
		"account_head": get_tax_account_head(tax, charge_type="sales_tax"),
		"description": (
			get_tax_account_description(tax) or f"{tax.get('title')} - {flt(tax.get('rate')) * 100.0:.2f}%"
		),
		"tax_amount": amount,
		"included_in_print_rate": 0,
		"cost_center": setting.cost_center,
		"dont_recompute_tax": 1,
		ITEM_WISE_TAX_KEY: {},
	}
	if item_code:
		row[ITEM_WISE_TAX_KEY] = {item_code: [flt(tax.get("rate")) * 100, amount]}
	return row


# --- shipping ---


def get_shipping_refund(refund) -> tuple[float, list[tuple[dict, float]]]:
	"""Refunded shipping (excluding tax) and its taxes as (tax_line, amount) pairs.

	Newer Admin API versions send ``refund_shipping_lines``; older ones report shipping refunds
	as ``order_adjustments`` of kind ``shipping_refund`` with negative amounts. Only one of the
	two is read so a refund is never counted twice.
	"""
	shipping_lines = refund.get("refund_shipping_lines") or []
	if shipping_lines:
		amount, taxes = 0.0, []
		for rsl in shipping_lines:
			shipping_line = rsl.get("shipping_line") or {}
			subtotal = flt(_shop_money(rsl.get("subtotal_amount_set")) or rsl.get("subtotal_amount"))
			amount += subtotal
			price = flt(shipping_line.get("price")) or subtotal
			ratio = subtotal / price if price else 0
			for tax in shipping_line.get("tax_lines") or []:
				taxes.append((tax, flt(flt(tax.get("price")) * ratio, 2)))
		return flt(amount, 2), taxes

	amount, tax_amount = 0.0, 0.0
	for adjustment in refund.get("order_adjustments") or []:
		if adjustment.get("kind") == "shipping_refund":
			amount += abs(flt(adjustment.get("amount")))
			tax_amount += abs(flt(adjustment.get("tax_amount")))

	taxes = [({"title": _("Shipping tax")}, flt(tax_amount, 2))] if tax_amount else []
	return flt(amount, 2), taxes


def get_shipping_refund_rows(amount, setting) -> list[dict]:
	if not amount:
		return []

	account = setting.default_shipping_charges_account
	if not account:
		frappe.throw(
			_("Set a Default Shipping Charges Account in Shopify Setting to credit refunded shipping.")
		)

	return [
		{
			"charge_type": "Actual",
			"account_head": account,
			"description": _("Shipping refund"),
			"tax_amount": -flt(amount),
			"cost_center": setting.cost_center,
			ITEM_WISE_TAX_KEY: {},
		}
	]


def _shipping_tax_rows(shipping_taxes, setting) -> list[dict]:
	return [_tax_row(tax, -amount, setting) for tax, amount in shipping_taxes if amount]


def _shop_money(money_set) -> float | None:
	if not money_set:
		return None
	return flt((money_set.get("shop_money") or {}).get("amount"))


# --- payment ---


def make_refund_payment(credit_note, refunded_amount, setting, refund) -> str:
	from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry

	credit_note.reload()
	outstanding = abs(flt(credit_note.outstanding_amount))

	if refunded_amount <= 0:
		return _("No money was refunded in Shopify yet, so no refund payment was made.")

	if not outstanding:
		return ""

	tolerance = AMOUNT_TOLERANCE + abs(flt(credit_note.get("rounding_adjustment")))
	if abs(refunded_amount - outstanding) > tolerance:
		return _(
			"Shopify refunded {0} but the credit note comes to {1}; record the refund payment manually."
		).format(refunded_amount, outstanding)

	posting_date = getdate(refund.get("processed_at") or refund.get("created_at")) or nowdate()

	from ecommerce_integrations.shopify.invoice import get_payment_account

	gateways = [
		t.get("gateway")
		for t in refund.get("transactions") or []
		if t.get("kind") == "refund" and t.get("status") == "success"
	]
	payment_entry = get_payment_entry(
		credit_note.doctype, credit_note.name, bank_account=get_payment_account(setting, gateways)
	)
	payment_entry.flags.ignore_mandatory = True
	payment_entry.reference_no = cstr(refund.get("id"))
	payment_entry.reference_date = posting_date
	payment_entry.posting_date = posting_date
	payment_entry.remarks = _("Shopify refund {0} for order {1}").format(
		refund.get("id"), credit_note.get(ORDER_NUMBER_FIELD) or refund.get("order_id")
	)
	payment_entry.insert(ignore_permissions=True)
	payment_entry.submit()

	return _("Refund payment {0}.").format(payment_entry.name)


# --- stock ---


def make_stock_return(order_id, refund, line_items, setting) -> str:
	"""Return restocked goods against the order's Delivery Notes."""
	from erpnext.stock.doctype.delivery_note.delivery_note import make_sales_return

	to_return = defaultdict(float)
	location_id = None
	for rli in line_items:
		if rli.get("restock_type") in RESTOCK_RETURN_TYPES:
			to_return[get_item_code(rli.get("line_item") or {})] += flt(rli.get("quantity"))
			location_id = location_id or rli.get("location_id")

	if not to_return:
		return ""

	delivery_notes = frappe.get_all(
		"Delivery Note",
		filters={ORDER_ID_FIELD: order_id, "docstatus": 1, "is_return": 0},
		pluck="name",
		order_by="posting_date asc, creation asc",
	)
	if not delivery_notes:
		return _("Restocked items were never delivered in ERPNext, so no stock return was needed.")

	warehouse = (
		setting.get_integration_to_erpnext_wh_mapping().get(cstr(location_id)) if location_id else None
	)
	refund_id = cstr(refund.get("id"))
	posting_date = getdate(refund.get("created_at")) or nowdate()

	created = []
	for dn_name in delivery_notes:
		if not any(qty > 0 for qty in to_return.values()):
			break

		stock_return = make_sales_return(dn_name)
		items = []
		for row in stock_return.items:
			pending = to_return.get(row.item_code, 0)
			qty = min(pending, abs(flt(row.qty)))
			if qty <= 0:
				continue

			row.qty = -qty
			row.stock_qty = -qty * flt(row.conversion_factor or 1)
			if warehouse:
				row.warehouse = warehouse
			to_return[row.item_code] -= qty
			items.append(row)

		if not items:
			continue

		stock_return.set("items", items)
		stock_return.set(FULLFILLMENT_ID_FIELD, "")
		stock_return.set(REFUND_ID_FIELD, refund_id)
		stock_return.naming_series = setting.delivery_note_series or stock_return.naming_series
		stock_return.set_posting_time = 1
		stock_return.posting_date = posting_date
		stock_return.flags.ignore_mandatory = True
		stock_return.insert(ignore_permissions=True)
		stock_return.submit()
		created.append(stock_return.name)

	note = _("Stock returned with {0}.").format(", ".join(created)) if created else ""
	leftover = {code: qty for code, qty in to_return.items() if qty > 0}
	if leftover:
		note += " " + _("Not returned (more than was delivered): {0}.").format(
			", ".join(f"{code} x {qty:g}" for code, qty in leftover.items())
		)
	return note.strip()
