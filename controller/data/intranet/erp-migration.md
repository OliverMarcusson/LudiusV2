---
title: ERP finance module migration FAQ
section: IT
owner: IT
updated: 2026-10-26
popular: true
tags: ERP, cloud, finance module, migration, cutover, role mapping, roles, access, permissions, error, error code, users, go-live
---
Cutover is planned after the October close. Finance users get their roles in the new module from the access list that IT and finance agree; nobody gets access that is not on the list.

Role mapping errors: ROLE-404 means the role on the list does not exist in the new module (check the spelling against the role catalogue); ROLE-409 means two roles conflict on segregation of duties (send it to the financial controller); AUTH-401 at login means the account has not been synced yet.

Report problems to the service desk with the error code, the user and a screenshot.
