# Manual creation: scoped Manager acceptance

Accepted candidate `8d2039f8efc5572726c70aafbf12d50d16558c69`, 2026-10-08.
This accepts the D36 creation correction and its synchronized shared test, before
the required combined core/UI check. Its parent is `020879eca62526652737f5d9dde5f2276903556c`;
the original implementation base is `862d989edf0fef0bc6c5100a051e936cb5afbcde`.

Fresh Astra MAX review 164 confirmed the production correction and found a
test timing assumption. Manager reproduced that failure. A fresh Astra MAX
builder corrected only the shared flow and evidence; fresh Astra MAX review 176
found no remaining in-scope defect. It verified 162 tracked files, the 160 frozen
paths and cumulative D36 scope, then passed the real source flow and an installed
flow with actual provisioning deliberately completed before returning the
unchanged first creation response. Production bytes remain identical to the
first candidate. The historical old-code receipt flow ran in review 164; review
176 inspected it without claiming a second execution.

Manager read the final correction and cumulative production change, confirmed
clean builder/reviewer heads and the pushed candidate, and matched all 56 package
files across Git, the independent wheel and installed package. A separate
Manager-authored installed diagnostic passed the corrected unmodified shared
flow while forcing the prior failing ordering. The receipt remained revision
0/null, the live record became revision 1/timestamp, only those two fields
differed, and the original response bytes/headers remained unchanged. Every
loaded core module came from the verified installed package.

The independent wheel SHA-256 is
`586a6de0522cf925933001f36286b5d903b764aa94174ede4438110f2e635c54`.
Manager evidence is retained at `manager-creation176/` in the recorded build run;
fresh review evidence is in `review-manual-creation-r2-astra-b505f522-l8exa5er/`.
The candidate's fourteen-operation/schema-5 base is historical: the next
[combined check](../MANUAL-CREATION-INTEGRATION.md) must preserve the accepted
fifteen-operation/schema-6 ownership increment and explicitly migrate its old-code
test installation. Those combined results are not claimed by this acceptance.

New creation records identity/default metadata and provisions authored files;
it never requests a CLI launch. Fresh start=true is refused, while committed
historical receipts replay unchanged. Terminal/native startup, inbox delivery,
assistant capture and VM/browser capability remain unimplemented by this change.
No native/provider turns, owner data or live Qorqut sessions were used. No
deployment or complete system gate is accepted.
