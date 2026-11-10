Non-Functional Requirements: Matatu Ticketing System
1. Purpose

This section defines the measurable or testable quality characteristics expected of the Matatu Ticketing System. It covers boarding, payment and settlement in Nairobi paratransit operations. 
The system is at requirements stage, so every target below is a proposed requirement. 

2. NFRs

NFR-001: Role-based secure access
Relevance: The system handles fare money, passenger phone numbers and crew earnings. Passengers, conductors/drivers, SACCO administrators, vehicle owners and auditors need different, limited views.
Acceptance criterion: Only authenticated users may access crew, admin and reporting functions. Each role sees only its permitted data: a conductor sees only the trips and tickets for their assigned vehicle, an owner sees only their own vehicles, and an auditor has read-only access. Sessions on shared crew phones expire after inactivity.
Compliance testing: Log in with invalid credentials and confirm access is blocked. Attempt to view another SACCO’s vehicle revenue with each role and verify denial. Confirm that an owner cannot edit fares and a conductor cannot view settlement splits.
Status: Proposed

NFR-002: Passenger data confidentiality and privacy compliance
Relevance: Passenger mobile numbers and travel patterns are personal data under Kenya’s Data Protection Act, 2019. That Act prohibits transfers of data outside Kenya unless there are adequate safeguards or the data subject has consented.
Acceptance criterion: Phone numbers are encrypted at rest and masked in all crew-facing screens (for example 07XX XXX 123). Only the minimum data needed for ticketing and dispute resolution is stored. The system never stores M-Pesa PINs or card details. Hosting location and any cross-border transfers are documented and meet the Act’s safeguards.
Compliance testing: Inspect the database and confirm phone numbers are not stored in plain text. Review the crew app and logs for unmasked numbers. Verify that a data-subject deletion or access request can be completed for a test passenger. Confirm the hosting region is documented.
Status: Proposed 
bowmanslaw

NFR-003: Ticket forgery and replay resistance
Relevance: A digital ticket is only trusted by crews if it cannot be faked, screenshotted and reused by another passenger.
Acceptance criterion: Every ticket carries a signed or non-guessable code tied to its route, vehicle and time window. A ticket validates successfully once for its intended trip and is rejected on second use. Altered or expired tickets are rejected.
Compliance testing: Scan a valid ticket twice and confirm the second scan fails. Edit a QR payload, use a ticket on a different vehicle or route, and use an expired ticket; confirm each is rejected. Attempt to generate valid codes by guessing and confirm it fails.
Status: Proposed

NFR-004: Payment-to-ticket latency
Relevance: Passengers pay while boarding or standing at a stage. If the ticket or confirmation is slow, crews and riders fall back to cash, and the system loses trust.
Acceptance criterion: The M-Pesa STK prompt reaches the passenger’s phone within 5 seconds of the request, subject to the network. Once the payment callback is received, the ticket and SMS confirmation are issued within 10 seconds at the 95th percentile.
Compliance testing: Run repeated test payments in the payment provider’s sandbox and then on live shortcode pilots. Measure the interval between callback receipt and ticket issue, and between the request and the prompt. Verify that p95 meets the target.
Status: Proposed

NFR-005: Boarding validation response time
Relevance: Crews validate many passengers in seconds at stages. Matatus have no fare gates, so slow scanning holds up the vehicle and gives crews a reason to skip validation.
Acceptance criterion: On a low-end Android device (for example 2 GB RAM, Android 8 or later), the crew app shall return a valid/invalid result within 2 seconds when online and within 1 second when validating offline.
Compliance testing: Time 200 validations on a reference low-end device under online, weak-signal and offline conditions. Confirm the 95th percentile meets the targets.
Status: Proposed

NFR-006: Offline operation and synchronisation
Relevance: Mobile coverage varies along routes, in tunnels, and at some termini, and a validation failure at the door erodes crew trust.
Acceptance criterion: The crew app shall validate tickets issued before loss of connectivity for at least 24 hours without a network connection. It shall queue validation events and sync them when connectivity returns, without data loss or duplicate usage records.
Compliance testing: Enable airplane mode on the crew device during a simulated trip and validate tickets. Reconnect and confirm all events sync exactly once. Test a ticket used offline on two devices and confirm the conflict is detected at sync.
Status: Proposed

NFR-007: Peak-hour load capacity
Relevance: Matatu demand is concentrated into morning and evening commuting peaks. Nairobi’s matatus carry over 3 million passengers daily (indicative figure from a technology overview). Payment requests arrive in bursts, not evenly.
Acceptance criterion: For the pilot scale (for example 500 vehicles), the platform shall sustain 2,000 payment initiations per minute during the 06:00–09:00 and 16:00–20:00 peaks without errors above 1% and without breaching NFR-004 or NFR-005. The architecture shall scale to ten times that load by adding capacity rather than redesigning.
Compliance testing: Run load tests that ramp from 100 to 2,000 and then 20,000 initiations per minute using simulated callbacks. Record error rate, latency and resource use, and verify that scaling is horizontal.
Status: Proposed 
envisioning

NFR-008: Availability
Relevance: Fare collection runs from early morning to late evening. Downtime during those hours directly stops revenue and pushes crews back to cash.
Acceptance criterion: The core services (payments, ticketing and validation) shall meet 99.5% monthly availability. Planned maintenance shall occur outside 05:00–22:00. Failure of an external dependency (such as a delayed M-Pesa callback) shall degrade gracefully and shall not crash the system.
Compliance testing: Monitor uptime over a month. Simulate a payment-provider outage and confirm the system queues requests and shows clear status messages. Verify that maintenance windows are respected.
Status: Proposed

NFR-009: Payment processing reliability and idempotency
Relevance: Mobile-money flows involve asynchronous callbacks that can arrive late, twice, or not at all. A passenger must never be charged without a ticket, nor receive two tickets for one payment. Typical Daraja integrations record a pending transaction, mark it successful only on a verified callback, run a scheduled check for stale pending requests, and keep one record when a duplicate callback arrives.
Acceptance criterion: Each payment shall produce exactly one ticket. A duplicate callback shall not create a second ticket. A pending payment with no callback shall be resolved by a status query within 5 minutes. Re-running the same set of transactions shall produce identical ticket and ledger outcomes.
Compliance testing: Replay the same callback three times and confirm one ticket. Drop a callback in the test environment and confirm the status check resolves it. Run a payment-to-ticket test dataset repeatedly and compare outputs.
Status: Proposed 
ecosire

NFR-010: Accuracy of reconciliation and settlement
Relevance: Owners, SACCOs and crews all depend on correct money splits. Errors or unexplained differences recreate the mistrust that cash handling already causes.
Acceptance criterion: On a curated benchmark dataset with known payments, refunds, reversals, duplicates and unmatched M-Pesa entries, the system shall classify every item correctly. Settlement splits (owner, SACCO, crew) shall sum exactly to collected revenue with defined rounding rules. Any unexplained variance shall be flagged and shall not be hidden.
Compliance testing: Compare system output against expected results for the benchmark dataset. Test splits with awkward amounts to verify rounding. Reconcile a sample day against the M-Pesa statement and confirm a zero or fully explained difference.
Status: Proposed

NFR-011: Usability for crews, passengers and low-literacy users
Relevance: Users range from smartphone-literate commuters to feature-phone users and crews with little training. The ITF labour study highlights training on cashless modes as an issue in its own right.
Acceptance criterion: All user-facing text shall be available in English and Kiswahili. A conductor shall complete a ticket validation in no more than two taps. A USSD payment shall take no more than four steps. A first-time passenger shall pay without prior training. Error messages shall state what to do next in plain language.
Compliance testing: Run walkthroughs with at least five conductors, five drivers and ten passengers (including feature-phone users). Record task completion rate and time, and confirm the step limits. Review messages for plain-language clarity in both languages.
Status: Proposed 
issuu

NFR-012: Network and device compatibility
Relevance: Passengers and crews use a wide range of devices and connection quality, and mobile data costs matter to crews.
Acceptance criterion: The system shall work on 2G/3G connections, on Android 8 or later with 2 GB RAM, and, for payment and ticket receipt, on feature phones via USSD and SMS. The crew app shall use a small data footprint per validation (target under 5 KB when online) and shall not drain a typical device battery faster than an agreed limit over a full shift.
Compliance testing: Throttle the network to 2G/3G profiles and run the full pay-and-validate flow. Measure data per validation. Run a full-shift battery test on a reference device. Test the USSD flow on a feature phone.
Status: Proposed

NFR-013: Data integrity and auditability
Relevance: Disputes between passengers, crews, owners and SACCOs are inevitable. The ITF study lists fare reversal fears and payment-message discrepancies among crews’ negative experiences, so the evidence trail must be reliable.
Acceptance criterion: Transactions, ticket events and admin changes shall be written to an append-only audit log. Corrections shall be made through new reversing entries, never by editing or deleting history. Each log entry shall record who, what, when and from which device.
Compliance testing: Attempt to edit or delete a past transaction and confirm it is blocked. Perform a reversal and verify both the original and the reversing entries are retained. Reconstruct a disputed trip from logs alone.
Status: Proposed 
issuu

NFR-014: Interoperability and open data
Relevance: Matatu workers know several competing cashless platforms, and fragmentation is a known problem. SACCOs also need to feed and use route data.
Acceptance criterion: The system shall expose a documented REST API. It shall export route and stop data in valid GTFS, including the “continuous stops” field used for flexible boarding. Adding another payment provider shall require only a new adapter, not changes to the ticketing or settlement logic.
Compliance testing: Validate the GTFS export with a standard GTFS validator. Integrate a mock second payment provider and confirm no change is needed outside the adapter. Test the API against its published documentation.
Status: Proposed 
issuu

NFR-015: Maintainability
Relevance: Fares, regulations and payment-provider APIs change over time, and the system must change with them.
Acceptance criterion: The system shall be organised into modules with clear responsibilities (payments, ticketing, validation, settlement, reporting, administration). A change to one module, such as a new fare rule, shall not require unrelated edits. Fare tables and split rules shall be configurable without code changes.
Compliance testing: Review module boundaries and run unit tests for each module. Change a fare rule through configuration only and confirm correct behaviour. Make a code change in one module and confirm other modules’ tests still pass.
Status: Proposed

NFR-016: Backup and recovery
Relevance: Fare and settlement records are the basis for crew pay, owner payouts and any later audit or dispute.
Acceptance criterion: Transaction and ledger data shall be backed up automatically, with a recovery point objective of 15 minutes and a recovery time objective of 4 hours. Records shall be retained for a defined period (proposed 7 years, to be confirmed against data-protection and tax record-keeping rules).
Compliance testing: Restore a backup into a test environment and verify the ledger totals match the source. Simulate loss of the primary database and measure recovery time. Verify the retention policy is enforced.
Status: Proposed