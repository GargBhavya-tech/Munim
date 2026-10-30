---
id: bank_verification
version: 1
title: Bank verification guide
scopes: [bank_verification]
review_status: DRAFT_UNREVIEWED
provenance: SYNTHETIC
---

## How bank verification works {#how_it_works}
The bank account is verified by a small test credit or by matching the account holder name. Verification usually finishes within a few minutes but can take longer when the bank is slow.

## Failure reasons {#failure_reasons}
TIMEOUT means the bank did not answer in time; trying again usually works. NAME_MISMATCH means the account holder name does not match the business or owner name; a retry will not fix it and the merchant should use an account in the right name. ACCOUNT_CLOSED means the bank reports the account as closed.

## Retrying {#retry}
For a TIMEOUT failure Munim can start the verification again. For any other reason Munim sends a self-serve link so the merchant can update the account in the app. Munim never takes account numbers in chat and never changes a settlement account.

## Using a different account {#other_account}
If a merchant asks to use another account, Munim sends the secure update link and explains that the change is made by the merchant in the app. A request to change the account inside the chat is declined and may be reviewed for safety.
