import frappe


def execute():
	"""Fill the new Item Name column on existing Ecommerce Items."""
	frappe.db.sql(
		"""update `tabEcommerce Item` ei
		join `tabItem` i on i.name = ei.erpnext_item_code
		set ei.item_name = i.item_name"""
	)
