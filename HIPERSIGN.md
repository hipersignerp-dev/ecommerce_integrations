# Hipersign extensions to the Shopify connector

Branch `hipersign-v16` is Frappe's `version-16` plus the changes below. `version-16` in this fork
mirrors upstream; merge it into `hipersign-v16` to take Frappe's updates.

New settings live in **Shopify Setting → Refunds, Fulfilment & Masters**. After deploying, open
Shopify Setting and **Save** once: that adds the new custom fields and registers the
`refunds/create` webhook.

| Feature | Setting | Code |
|---|---|---|
| Shopify refund → credit note (Sales Invoice return), stock return for restocked items that were delivered, refund Payment Entry | Create Credit Notes for Shopify Refunds (on by default) | `shopify/refund.py` |
| Cancelled orders: documents stamped `cancelled (<payment status>)`, Sales Order closed so it stops reserving stock (unpaid, unshipped orders are still cancelled outright) | – | `shopify/order.py::cancel_order` |
| Delivery Note submitted in ERPNext → Shopify fulfillment (Transport Receipt No = tracking number, Transporter Name = carrier), without a duplicate DN coming back from the webhook | Fulfil Shopify Order on Delivery Note Submit, Send Shipping Email to Customer | `shopify/fulfillment_push.py` |
| Company link and HSN code on Customers, Suppliers, Items and Item Groups created by the sync | Company Field on Masters (e.g. `custom_company`), Default HSN Code | `shopify/master_defaults.py` |
| Re-register webhooks when the app subscribes to new topics | – | `shopify_setting.py::_handle_webhooks` |

Not automated (logged for a person): refunds with no refunded items (amount-only or
shipping-only), and refunds whose amount differs from the credit note (the credit note is made,
the refund payment is left for manual entry).
