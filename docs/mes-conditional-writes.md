# Real MES systems and conditional writes

Research as of October 28, 2026, from public vendor documentation only.

## What a conditional write means here

A conditional write is a change the MES makes only if the record is still in the state the caller checked. Otherwise the MES rejects it. In the demo, the executor sends the version it verified, and the MES releases only if the order is still at that version and has no quality hold. Without that, a hold placed between the last check and the release can be missed.

Two kinds of support count:

- **Optimistic concurrency in the API.** The write carries a version, timestamp or ETag (`If-Match`), and a stale one is rejected.
- **The MES's own rules.** The MES itself refuses to release or move an order that is on hold. This covers holds, but not other changes, such as a revised quantity or part revision.

## The list

Three of the five systems document no conditional write in the API pages reviewed, and the other two couldn't be reviewed. SAP and Critical Manufacturing document holds that stop a release or a move by themselves.

| System | Hold or status rule stops it by itself? | Conditional write documented? | What the public docs show |
| --- | --- | --- | --- |
| [SAP Digital Manufacturing](https://learning.sap.com/courses/configuring-sap-digital-manufacturing-for-execution-basic-data-and-configuration/creating-and-releasing-orders) (cloud successor to SAP ME) | Yes | Not confirmed | An order whose release is on hold can't be released. SAP's API reference wasn't reachable for this check, so the Order API is a question for SAP. |
| [Critical Manufacturing MES](https://help.criticalmanufacturing.com/userguide/business-data/material/hold_material/) | Yes | Not documented in the API pages reviewed | "A Material on hold cannot be moved into/from any Step and Resource." The [API reference](https://developer.criticalmanufacturing.com/11.2/reference/index.html) pages checked don't mention versions or stale-update checks. |
| [GE Vernova Proficy Plant Applications](https://www.ge.com/digital/documentation/proficy-plant-applications/version81/process_service_docs/productionscheduler-app-service-0.2.1-documentation.html) | Partly | Not documented in the API pages reviewed | Each process order lists its allowed status changes. `PUT /api/processOrders/{orderId}` takes no version, ETag or `If-Match`. It can return [409 Conflict](https://www.gevernova.com/software/documentation/proficy-plant-applications/version2023/pa_rest_api/c_examples_error_codes.html), but the docs don't say when. |
| [Tulip](https://support.tulip.co/apidocs/update-a-record-of-a-tulip-table) | Not part of the API | Not documented in the API pages reviewed | Updating a table record documents no version, ETag or `If-Match`. |
| [Siemens Opcenter Execution](https://developer.siemens.com/guidelines/api-guidelines/rest/common-operations.html) | Not checked | Not confirmed | Opcenter's API reference wasn't publicly readable for this check. Siemens' general REST guidelines say servers *may* support ETag with `If-Match` (rule 803.6). |

Not checked yet: AVEVA MES (its doc pages didn't load for this check), Rockwell Plex and FactoryTalk ProductionCentre, and DELMIA Apriso. "Not documented in the API pages reviewed" means only that: the product may still support it. "Not confirmed" means the API reference couldn't be reviewed for this check.

## What this means for an integration

The first question for any prospect's MES vendor is whether a release can be made conditional on the order's version or last-changed time. Public docs mostly don't answer it. Depending on the answer, there are three ways in:

1. **The MES supports it.** The executor sends the version it checked with the release, as the demo does.
2. **The MES has server-side rules.** A rule inside the MES re-checks the order in the same transaction as the release. Critical Manufacturing, for example, documents DEE action rules that extend its backend.
3. **Neither.** The plant relies on the MES's own hold and status checks. **Limitation: a race window remains.** Any other change between the executor's last check and the MES write, such as a revised quantity or a withdrawn approval, can still be missed. Accepting that window, or closing it another way, is a deployment decision for the plant to make and record before go-live. The demo doesn't resolve it.

## Sources

- [SAP Learning: Creating and Releasing Orders (SAP Digital Manufacturing)](https://learning.sap.com/courses/configuring-sap-digital-manufacturing-for-execution-basic-data-and-configuration/creating-and-releasing-orders)
- [Critical Manufacturing: Hold Material](https://help.criticalmanufacturing.com/userguide/business-data/material/hold_material/)
- [Critical Manufacturing: MES API Reference, 11.2](https://developer.criticalmanufacturing.com/11.2/reference/index.html)
- [GE Vernova Proficy Plant Applications 8.1: Production Scheduler service](https://www.ge.com/digital/documentation/proficy-plant-applications/version81/process_service_docs/productionscheduler-app-service-0.2.1-documentation.html)
- [GE Vernova Proficy Plant Applications 2023: REST API error codes](https://www.gevernova.com/software/documentation/proficy-plant-applications/version2023/pa_rest_api/c_examples_error_codes.html)
- [Tulip: Update a record of a Tulip Table](https://support.tulip.co/apidocs/update-a-record-of-a-tulip-table)
- [Siemens Xcelerator REST API Guidelines: Common Operations](https://developer.siemens.com/guidelines/api-guidelines/rest/common-operations.html)
