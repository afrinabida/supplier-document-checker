# Aamar Garments Ltd (fictional): supplier document rules

Use these rules to decide each supplier pack. The purchase request is `purchase-request.csv`.

## Reject (any one of these)
1. Unit price is more than 3% above the maximum unit price in the purchase request.
2. Advance payment is more than 20%.
3. The contract has no late-delivery penalty clause.
4. The supplier's TIN or BIN is missing.
5. Delivery is promised later than 45 days after the purchase order.

## Needs a person to check (any one of these, if nothing above applies)
1. Unit price is above the maximum, but by no more than 3%.
2. Quantity differs from the quantity requested by more than 5%.
3. Payment terms are unclear, or outside 30 to 60 days after delivery.
4. Delivery is promised in 31 to 45 days.
5. VAT is not shown separately at 15%.
6. The numbers do not add up (for example line total or subtotal is wrong).
7. The quotation is valid for less than 30 days.

## Approve
None of the above applies.

## Other notes
- The late-delivery penalty must be at least 0.5% of order value per week of delay.
- Always quote the exact text from the document that supports your decision.
- If you are not sure, the answer is "Needs a person to check". Never guess.
