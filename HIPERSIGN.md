# Hipersign extensions to the Shopify connector

Branch `hipersign-v16` is Frappe's `version-16` plus the changes below. `version-16` in this fork
mirrors upstream; merge it into `hipersign-v16` to take Frappe's updates.

After deploying (`bench migrate`), open **Shopify Setting** and **Save** once: that adds the new
custom fields and registers the new webhooks (refunds, products, customers).

## What syncs

| Flow | Direction | When | Setting | Code |
|---|---|---|---|---|
| Order → Sales Order, payment → Sales Invoice + Payment Entry, fulfilment → Delivery Note | Shopify → ERPNext | real time (webhook) | standard | `order.py`, `invoice.py`, `fulfillment.py` |
| Refund → credit note (Sales Invoice return), stock return for restocked delivered items, refund Payment Entry | Shopify → ERPNext | real time | Create Credit Notes for Shopify Refunds | `refund.py` |
| Cancellation → documents stamped `cancelled (<payment status>)`, Sales Order closed (unpaid & unshipped: cancelled) | Shopify → ERPNext | real time | – | `order.py::cancel_order` |
| Delivery Note → Shopify fulfilment with tracking (Transport Receipt No / Transporter Name) | ERPNext → Shopify | on submit | Fulfil Shopify Order on Delivery Note Submit | `fulfillment_push.py` |
| Stock levels | ERPNext → Shopify | seconds after every stock posting / reservation change, plus the schedule | Instant Stock Sync | `inventory_realtime.py` |
| Products, variants, archived status, prices | Shopify → ERPNext | real time | Sync Product Changes from Shopify, Shopify Price List | `product_sync.py` |
| Item templates with variants (size/colour) as one product with options; price list changes | ERPNext → Shopify | on save | Push Variant Items as Shopify Options, Shopify Price List | `product_sync.py` |
| Customer name / email / phone / address changes, deletions | Shopify → ERPNext | real time | Sync Customer Changes from Shopify | `customer_sync.py` |
| Missed webhooks: orders, payments, fulfilments, cancellations, refunds; full stock re-send | Shopify → ERPNext | nightly | Nightly Reconciliation | `reconcile.py` |
| Payments and refunds per gateway (Razorpay, COD, …) to their own clearing accounts | – | – | Payment Gateway Accounts | `invoice.py::get_payment_account` |
| Company link and HSN code on synced Customers, Suppliers, Items, Item Groups | – | – | Company Field on Masters, Default HSN Code | `master_defaults.py` |

## Rules worth knowing

- **Echo protection.** Writes that come from Shopify are flagged and never pushed back; pushes
  to Shopify mark themselves so the webhook that comes back doesn't duplicate anything.
- **Masters.** ERPNext is master for stock. Customers are mastered in Shopify (created with
  their first order, then kept updated). Products and prices sync both ways, last change wins.
- **Not automated, logged for a person:** refunds with no refunded items (amount-only or
  shipping-only), refund payments whose amount differs from the credit note, Shopify products
  that change from simple to multi-option after being linked.
- Don't enable both *Push Variant Items as Shopify Options* and the standard *Upload ERPNext
  Variants as Shopify Items*.
- Shopify Payments payouts are not used: it isn't available to Indian stores. Settlements from
  Razorpay etc. are reconciled through the gateway clearing accounts.
