// Copyright (c) 2021, Frappe and contributors
// For license information, please see LICENSE

frappe.provide("ecommerce_integrations.shopify.shopify_setting");

frappe.ui.form.on("Shopify Setting", {
	onload: function (frm) {
		frappe.call({
			method: "ecommerce_integrations.utils.naming_series.get_series",
			callback: function (r) {
				$.each(r.message, (key, value) => {
					set_field_options(key, value);
				});
			},
		});
	},

	fetch_shopify_locations: function (frm) {
		frappe.call({
			doc: frm.doc,
			method: "update_location_table",
			callback: (r) => {
				if (!r.exc) refresh_field("shopify_warehouse_mapping");
			},
		});
	},

	refresh: function (frm) {
		frm.add_custom_button(__("Import Products"), function () {
			frappe.set_route("shopify-import-products");
		});
		frm.add_custom_button(__("View Logs"), () => {
			frappe.set_route("List", "Ecommerce Integration Log", {
				integration: "Shopify",
			});
		});
		if (frm.doc.enable_shopify && !frm.is_dirty()) {
			frm.add_custom_button(
				__("Run Reconciliation Now"),
				() => {
					frappe.call({
						method: "ecommerce_integrations.shopify.reconcile.run_now",
						callback: () =>
							frappe.show_alert({
								message: __(
									"Reconciliation queued. The summary appears in the Ecommerce Integration Log.",
								),
								indicator: "green",
							}),
					});
				},
				__("Sync"),
			);
			frm.add_custom_button(
				__("Resend All Stock to Shopify"),
				() => {
					frappe.call({
						method: "ecommerce_integrations.shopify.inventory_realtime.push_all_stock_now",
						callback: () =>
							frappe.show_alert({
								message: __("Stock push queued for every linked item."),
								indicator: "green",
							}),
					});
				},
				__("Sync"),
			);
		}
		frm.trigger("setup_queries");
	},

	setup_queries: function (frm) {
		const warehouse_query = () => {
			return {
				filters: {
					company: frm.doc.company,
					is_group: 0,
					disabled: 0,
				},
			};
		};
		frm.set_query("warehouse", warehouse_query);
		frm.set_query(
			"erpnext_warehouse",
			"shopify_warehouse_mapping",
			warehouse_query,
		);

		frm.set_query("price_list", () => {
			return {
				filters: {
					selling: 1,
				},
			};
		});

		frm.set_query("cost_center", () => {
			return {
				filters: {
					company: frm.doc.company,
					is_group: "No",
				},
			};
		});

		frm.set_query("cash_bank_account", () => {
			return {
				filters: [
					["Account", "account_type", "in", ["Cash", "Bank"]],
					["Account", "root_type", "=", "Asset"],
					["Account", "is_group", "=", 0],
					["Account", "company", "=", frm.doc.company],
				],
			};
		});

		const tax_query = () => {
			return {
				query: "erpnext.controllers.queries.tax_account_query",
				filters: {
					account_type: ["Tax", "Chargeable", "Expense Account"],
					company: frm.doc.company,
				},
			};
		};

		frm.set_query("tax_account", "taxes", tax_query);
		frm.set_query("default_sales_tax_account", tax_query);
		frm.set_query("default_shipping_charges_account", tax_query);
	},
});
