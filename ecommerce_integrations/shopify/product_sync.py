# Copyright (c) 2026, Hipersign ERP and contributors
# For license information, please see LICENSE
"""Two-way product, variant and price sync.

Shopify -> ERPNext (products/create, products/update, products/delete webhooks)
	New products are created as Items (templates + variants for multi-option products).
	Edits update item name, description, archived status and prices; new variants are added,
	variants removed in Shopify are disabled; deleted products are disabled.
	Prices go to the "Shopify Price List" chosen in Shopify Setting (and the Item's
	Shopify Selling Rate).

ERPNext -> Shopify (Item / Item Price hooks, background jobs)
	Templates with variants (e.g. a shirt in sizes and colours) are pushed as ONE Shopify
	product with options and variants using productSet: title, description, product type,
	options, variant SKUs (= item codes), prices from the Shopify Price List and, for new
	variants, opening stock per mapped location. Price changes on simple items are pushed to
	their Shopify variant. (Simple items themselves are pushed by the standard connector.)

Echo protection: everything written from a Shopify webhook runs with
frappe.flags.in_shopify_product_sync set and Items flagged from_integration, and the ERPNext
hooks ignore those writes, so a change never bounces between the two systems.
"""

import frappe
from frappe import _
from frappe.utils import cint, cstr, flt

from ecommerce_integrations.ecommerce_integrations.doctype.ecommerce_item import ecommerce_item
from ecommerce_integrations.shopify.constants import (
	ITEM_SELLING_RATE_FIELD,
	MODULE_NAME,
	SETTING_DOCTYPE,
	SHOPIFY_VARIANTS_ATTR_LIST,
)
from ecommerce_integrations.shopify.master_defaults import belongs_to_shopify_company
from ecommerce_integrations.shopify.utils import create_shopify_log

PUSH_PRODUCT_METHOD = "ecommerce_integrations.shopify.product_sync.push_product_with_variants"
PUSH_PRICE_METHOD = "ecommerce_integrations.shopify.product_sync.push_simple_item_price"

PRODUCT_SET_MUTATION = """
mutation ProductSet($input: ProductSetInput!, $identifier: ProductSetIdentifiers) {
  productSet(input: $input, identifier: $identifier, synchronous: true) {
    product {
      legacyResourceId
      variants(first: 250) { nodes { legacyResourceId sku } }
    }
    userErrors { field message }
  }
}
"""

VARIANT_PRICE_MUTATION = """
mutation VariantPrice($productId: ID!, $variants: [ProductVariantsBulkInput!]!) {
  productVariantsBulkUpdate(productId: $productId, variants: $variants) {
    productVariants { id price }
    userErrors { field message }
  }
}
"""


# ============================================================================================
# Shopify -> ERPNext
# ============================================================================================


def sync_product_webhook(payload, request_id=None):
	"""products/create and products/update."""
	frappe.set_user("Administrator")
	frappe.flags.request_id = request_id
	setting = frappe.get_doc(SETTING_DOCTYPE)

	if not cint(setting.get("sync_products_from_shopify")):
		create_shopify_log(status="Invalid", message=_("Product sync from Shopify is disabled."))
		return

	previous = frappe.flags.in_shopify_product_sync
	frappe.flags.in_shopify_product_sync = True
	try:
		message = apply_shopify_product(payload, setting)
	except Exception as e:
		create_shopify_log(status="Error", exception=e, rollback=True)
	else:
		create_shopify_log(status="Success", message=message)
	finally:
		frappe.flags.in_shopify_product_sync = previous


def delete_product_webhook(payload, request_id=None):
	"""products/delete: disable the linked items (they may have stock and history)."""
	frappe.set_user("Administrator")
	frappe.flags.request_id = request_id

	previous = frappe.flags.in_shopify_product_sync
	frappe.flags.in_shopify_product_sync = True
	try:
		item_codes = frappe.get_all(
			"Ecommerce Item",
			filters={"integration": MODULE_NAME, "integration_item_code": cstr(payload.get("id"))},
			pluck="erpnext_item_code",
		)
		for item_code in item_codes:
			_update_item(item_code, {"disabled": 1})
	except Exception as e:
		create_shopify_log(status="Error", exception=e, rollback=True)
	else:
		create_shopify_log(status="Success", message=_("Disabled {0}").format(", ".join(item_codes) or "-"))
	finally:
		frappe.flags.in_shopify_product_sync = previous


def apply_shopify_product(product, setting) -> str:
	from ecommerce_integrations.shopify.product import ShopifyProduct, _has_variants

	product_id = cstr(product.get("id"))
	variants = product.get("variants") or []

	if not ecommerce_item.is_synced(MODULE_NAME, product_id):
		skus = [cstr(v.get("sku")) for v in variants]
		if skus and all(sku and frappe.db.exists("Item", sku) for sku in skus):
			# products ERPNext just pushed: the push job links them, don't create duplicates
			return _("Shopify product {0} was pushed from ERPNext; nothing to create.").format(product_id)

		ShopifyProduct(product_id).sync_product()
		_apply_prices(product, setting)
		return _("Created items for Shopify product {0}.").format(product_id)

	notes = []
	has_variants = _has_variants(product)
	title = cstr(product.get("title")).strip()
	main_values = {
		"item_name": title,
		"description": product.get("body_html") or title,
		"disabled": 1 if product.get("status") == "archived" else 0,
	}

	if has_variants:
		template = ecommerce_item.get_erpnext_item_code(MODULE_NAME, product_id, has_variants=1)
		if not template:
			frappe.throw(
				_(
					"Shopify product {0} now has variants but is linked to a simple item in ERPNext. "
					"Link or recreate it manually."
				).format(product_id)
			)
		_update_item(template, main_values)

		missing = [
			v
			for v in variants
			if not ecommerce_item.get_erpnext_item_code(MODULE_NAME, product_id, variant_id=cstr(v.get("id")))
		]
		if missing:
			shopify_product = ShopifyProduct(product_id, has_variants=1)
			attributes = shopify_product._create_attribute(product)
			shopify_product._create_item_variants(product, setting.warehouse, attributes)
			notes.append(_("added {0} new variant(s)").format(len(missing)))

		for variant in variants:
			item_code = ecommerce_item.get_erpnext_item_code(
				MODULE_NAME, product_id, variant_id=cstr(variant.get("id"))
			)
			if item_code:
				_update_item(
					item_code,
					{
						"item_name": f"{title}-{variant.get('title')}",
						"disabled": main_values["disabled"],
					},
				)

		# variants deleted in Shopify
		live_ids = {cstr(v.get("id")) for v in variants}
		for row in frappe.get_all(
			"Ecommerce Item",
			filters={"integration": MODULE_NAME, "integration_item_code": product_id, "has_variants": 0},
			fields=["erpnext_item_code", "variant_id"],
		):
			if row.variant_id and row.variant_id not in live_ids:
				_update_item(row.erpnext_item_code, {"disabled": 1})
				notes.append(_("disabled removed variant {0}").format(row.erpnext_item_code))
	else:
		item_code = ecommerce_item.get_erpnext_item_code(
			MODULE_NAME, product_id, variant_id=cstr(variants[0].get("id")) if variants else None
		)
		if item_code:
			_update_item(item_code, main_values)

	price_changes = _apply_prices(product, setting)
	if price_changes:
		notes.append(_("{0} price(s) updated").format(price_changes))

	return _("Updated Shopify product {0}{1}.").format(product_id, (": " + ", ".join(notes)) if notes else "")


def _apply_prices(product, setting) -> int:
	"""Write each variant's Shopify price to the Shopify Price List and Shopify Selling Rate."""
	product_id = cstr(product.get("id"))
	changed = 0
	for variant in product.get("variants") or []:
		item_code = ecommerce_item.get_erpnext_item_code(
			MODULE_NAME, product_id, variant_id=cstr(variant.get("id"))
		)
		if not item_code:
			continue

		price = flt(variant.get("price"))
		if _update_item(item_code, {ITEM_SELLING_RATE_FIELD: price}):
			changed += 1
		if setting.get("shopify_price_list") and _set_item_price(
			item_code, setting.shopify_price_list, price
		):
			changed += 1
	return changed


def _update_item(item_code, values) -> bool:
	"""Save only real changes; flagged so the standard upload hook doesn't echo them back."""
	item = frappe.get_doc("Item", item_code)
	changed = {
		key: value
		for key, value in values.items()
		if item.meta.has_field(key) and cstr(item.get(key)).strip() != cstr(value).strip()
	}
	if not changed:
		return False

	item.update(changed)
	item.flags.from_integration = True
	item.flags.ignore_mandatory = True
	item.save(ignore_permissions=True)
	return True


def _set_item_price(item_code, price_list, rate) -> bool:
	name = frappe.db.get_value("Item Price", {"item_code": item_code, "price_list": price_list}, "name")
	if name:
		if flt(frappe.db.get_value("Item Price", name, "price_list_rate")) == flt(rate):
			return False
		doc = frappe.get_doc("Item Price", name)
		doc.price_list_rate = rate
	else:
		doc = frappe.get_doc(
			{
				"doctype": "Item Price",
				"item_code": item_code,
				"price_list": price_list,
				"price_list_rate": rate,
			}
		)
	doc.flags.ignore_permissions = True
	doc.save()
	return True


# ============================================================================================
# ERPNext -> Shopify
# ============================================================================================


def on_item_update(doc, method=None):
	"""Item on_update: push templates/variants as one Shopify product with options."""
	if _is_echo(doc) or not (doc.has_variants or doc.variant_of):
		return
	if not _push_enabled() or not belongs_to_shopify_company(doc):
		return

	template = doc.name if doc.has_variants else doc.variant_of
	_queue_product_push(template)


def on_item_price_update(doc, method=None):
	"""Item Price on_update / after_insert: push price changes on the Shopify price list."""
	if _is_echo(doc) or not doc.selling:
		return

	setting = frappe.get_cached_doc(SETTING_DOCTYPE)
	if not setting.is_enabled() or doc.price_list != setting.get("shopify_price_list"):
		return

	item = frappe.db.get_value("Item", doc.item_code, ["has_variants", "variant_of"], as_dict=True)
	if not item or not belongs_to_shopify_company(doc.item_code, setting):
		return

	if (item.has_variants or item.variant_of) and _push_enabled():
		_queue_product_push(doc.item_code if item.has_variants else item.variant_of)
	elif not item.has_variants and frappe.db.exists(
		"Ecommerce Item", {"integration": MODULE_NAME, "erpnext_item_code": doc.item_code}
	):
		frappe.enqueue(
			PUSH_PRICE_METHOD,
			queue="short",
			job_id=f"shopify_price_push::{doc.item_code}",
			deduplicate=True,
			enqueue_after_commit=True,
			item_code=doc.item_code,
		)


def _is_echo(doc) -> bool:
	return bool(
		doc.flags.from_integration
		or frappe.flags.in_shopify_product_sync
		or frappe.flags.in_import
		or frappe.flags.in_migrate
		or frappe.flags.in_install
	)


def _push_enabled() -> bool:
	setting = frappe.get_cached_doc(SETTING_DOCTYPE)
	return bool(
		setting.is_enabled()
		and cint(setting.upload_erpnext_items)
		and cint(setting.get("push_variant_products"))
		# the standard "variants as separate products" mode would fight with this one
		and not cint(setting.upload_variants_as_items)
	)


def _queue_product_push(template):
	frappe.enqueue(
		PUSH_PRODUCT_METHOD,
		queue="short",
		timeout=600,
		job_id=f"shopify_product_push::{template}",
		deduplicate=True,
		enqueue_after_commit=True,
		template=template,
	)


def push_product_with_variants(template):
	from frappe.utils.synchronization import filelock

	frappe.set_user("Administrator")
	frappe.flags.request_id = None
	try:
		# Saving a template re-saves its variants, which queues several pushes; dedup only
		# stops queued duplicates, so serialise the running ones and commit the product link
		# before the next push reads it (otherwise two pushes each create a product).
		with filelock(f"shopify_product_push_{frappe.scrub(template)}", timeout=120):
			message = _push_product(template)
			frappe.db.commit()
	except Exception as e:
		frappe.db.rollback()
		create_shopify_log(
			status="Error",
			method=PUSH_PRODUCT_METHOD,
			request_data={"template": template},
			exception=e,
			make_new=True,
		)
	else:
		create_shopify_log(
			status="Success",
			method=PUSH_PRODUCT_METHOD,
			request_data={"template": template},
			message=message,
			make_new=True,
		)
		from ecommerce_integrations.shopify.inventory_realtime import queue_inventory_push

		queue_inventory_push()


def _push_product(template_code) -> str:
	from ecommerce_integrations.shopify.fulfillment_push import graphql_session, run_graphql

	setting = frappe.get_doc(SETTING_DOCTYPE)
	template = frappe.get_doc("Item", template_code)

	attribute_names = [row.attribute for row in template.attributes]
	if len(attribute_names) > len(SHOPIFY_VARIANTS_ATTR_LIST):
		frappe.throw(
			_("Shopify allows at most 3 options; {0} has {1}.").format(template_code, len(attribute_names))
		)

	variants = frappe.get_all(
		"Item",
		filters={"variant_of": template_code, "disabled": 0},
		fields=["name", "is_stock_item", ITEM_SELLING_RATE_FIELD],
		order_by="creation asc",
	)
	if not variants:
		frappe.throw(_("{0} has no enabled variants to push.").format(template_code))

	product_id = frappe.db.get_value(
		"Ecommerce Item",
		{"integration": MODULE_NAME, "erpnext_item_code": template_code, "has_variants": 1},
		"integration_item_code",
	)

	used_values = {name: [] for name in attribute_names}
	variant_inputs = []
	for variant in variants:
		values = dict(
			frappe.get_all(
				"Item Variant Attribute",
				filters={"parent": variant.name, "parenttype": "Item"},
				fields=["attribute", "attribute_value"],
				as_list=True,
			)
		)
		option_values = []
		for name in attribute_names:
			value = cstr(values.get(name))
			if not value:
				frappe.throw(_("Variant {0} has no value for {1}.").format(variant.name, name))
			if value not in used_values[name]:
				used_values[name].append(value)
			option_values.append({"optionName": name, "name": value})

		variant_input = {
			"optionValues": option_values,
			"price": _get_price(variant, template, setting),
			"inventoryItem": {"sku": variant.name, "tracked": bool(variant.is_stock_item)},
		}

		variant_id = frappe.db.get_value(
			"Ecommerce Item",
			{"integration": MODULE_NAME, "erpnext_item_code": variant.name},
			"variant_id",
		)
		if variant_id:
			variant_input["id"] = f"gid://shopify/ProductVariant/{variant_id}"
		elif cint(setting.update_erpnext_stock_levels_to_shopify):
			variant_input["inventoryQuantities"] = _opening_quantities(variant.name, setting)

		variant_inputs.append(variant_input)

	product_input = {
		"title": template.item_name,
		"descriptionHtml": template.description or template.item_name,
		"productType": template.item_group,
		"productOptions": [
			{"name": name, "values": [{"name": v} for v in used_values[name]]} for name in attribute_names
		],
		"variants": variant_inputs,
	}
	if template.disabled:
		product_input["status"] = "ARCHIVED"
	elif not product_id:
		product_input["status"] = "ACTIVE" if cint(setting.sync_new_item_as_active) else "DRAFT"

	variables = {"input": product_input}
	if product_id:
		variables["identifier"] = {"id": f"gid://shopify/Product/{product_id}"}

	with graphql_session(setting) as client:
		result = run_graphql(client, PRODUCT_SET_MUTATION, variables)["productSet"]

	if result.get("userErrors"):
		frappe.throw("; ".join(f"{e.get('field')}: {e.get('message')}" for e in result["userErrors"]))

	product = result["product"]
	_link_ecommerce_items(template, product)

	return _("{0} {1} in Shopify as product {2} with {3} variant(s).").format(
		template_code,
		"updated" if product_id else "created",
		product["legacyResourceId"],
		len(variant_inputs),
	)


def _get_price(variant, template, setting) -> str:
	price_list = setting.get("shopify_price_list")
	rate = None
	if price_list:
		rate = frappe.db.get_value(
			"Item Price", {"item_code": variant.name, "price_list": price_list}, "price_list_rate"
		) or frappe.db.get_value(
			"Item Price", {"item_code": template.name, "price_list": price_list}, "price_list_rate"
		)
	rate = rate or variant.get(ITEM_SELLING_RATE_FIELD) or template.get(ITEM_SELLING_RATE_FIELD) or 0
	return f"{flt(rate):.2f}"


def _opening_quantities(item_code, setting) -> list[dict]:
	quantities = []
	for warehouse, location_id in setting.get_erpnext_to_integration_wh_mapping().items():
		bin_qty = frappe.db.get_value(
			"Bin",
			{"item_code": item_code, "warehouse": warehouse},
			["actual_qty", "reserved_qty"],
			as_dict=True,
		)
		available = cint(flt(bin_qty.actual_qty) - flt(bin_qty.reserved_qty)) if bin_qty else 0
		quantities.append(
			{
				"locationId": f"gid://shopify/Location/{location_id}",
				"name": "available",
				"quantity": available,
			}
		)
	return quantities


def _link_ecommerce_items(template, product):
	product_id = cstr(product["legacyResourceId"])

	if not frappe.db.exists(
		"Ecommerce Item", {"integration": MODULE_NAME, "erpnext_item_code": template.name}
	):
		frappe.get_doc(
			{
				"doctype": "Ecommerce Item",
				"integration": MODULE_NAME,
				"erpnext_item_code": template.name,
				"integration_item_code": product_id,
				"has_variants": 1,
				"variant_id": "",
			}
		).insert(ignore_permissions=True)

	for node in product["variants"]["nodes"]:
		item_code = cstr(node.get("sku"))
		if not item_code or not frappe.db.exists("Item", item_code):
			continue
		if frappe.db.exists("Ecommerce Item", {"integration": MODULE_NAME, "erpnext_item_code": item_code}):
			continue
		frappe.get_doc(
			{
				"doctype": "Ecommerce Item",
				"integration": MODULE_NAME,
				"erpnext_item_code": item_code,
				"integration_item_code": product_id,
				"variant_id": cstr(node["legacyResourceId"]),
				"sku": item_code,
				"variant_of": template.name,
				"has_variants": 0,
			}
		).insert(ignore_permissions=True)


def push_simple_item_price(item_code):
	"""Price change on a simple (non-variant) Shopify item."""
	from ecommerce_integrations.shopify.fulfillment_push import graphql_session, run_graphql

	frappe.set_user("Administrator")
	frappe.flags.request_id = None
	setting = frappe.get_doc(SETTING_DOCTYPE)

	try:
		link = frappe.db.get_value(
			"Ecommerce Item",
			{"integration": MODULE_NAME, "erpnext_item_code": item_code},
			["integration_item_code", "variant_id"],
			as_dict=True,
		)
		rate = frappe.db.get_value(
			"Item Price",
			{"item_code": item_code, "price_list": setting.shopify_price_list},
			"price_list_rate",
		)
		if not link or not link.variant_id or rate is None:
			return

		with graphql_session(setting) as client:
			result = run_graphql(
				client,
				VARIANT_PRICE_MUTATION,
				{
					"productId": f"gid://shopify/Product/{link.integration_item_code}",
					"variants": [
						{"id": f"gid://shopify/ProductVariant/{link.variant_id}", "price": f"{flt(rate):.2f}"}
					],
				},
			)["productVariantsBulkUpdate"]
		if result.get("userErrors"):
			frappe.throw("; ".join(e["message"] for e in result["userErrors"]))
	except Exception as e:
		frappe.db.rollback()
		create_shopify_log(
			status="Error",
			method=PUSH_PRICE_METHOD,
			request_data={"item": item_code},
			exception=e,
			make_new=True,
		)
	else:
		create_shopify_log(
			status="Success",
			method=PUSH_PRICE_METHOD,
			request_data={"item": item_code},
			message=_("Price of {0} set to {1} in Shopify.").format(item_code, rate),
			make_new=True,
		)
