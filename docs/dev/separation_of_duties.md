# Separation of duties (LP-32, HECVAT APPL-05)

| Role | Holds | Stored as |
| --- | --- | --- |
| Owner | Both administrator roles; alone can grant both | `is_admin` (LlamaBot), `admin` (Rails) |
| Security admin | Users, role grants, sensitive-data rules, audit log | `is_security_admin`, `security_admin` |
| System admin | Settings, env vars, agent modes, deploys, git, scheduled jobs | `is_system_admin`, `system_admin` |
| Standard | Everyone else | |

Existing admins become owners, so an ordinary box changes nothing until someone is given a narrower role.

## Where it is enforced

- **LlamaBot, route boundary:** `permissions.route_allowed`, called from the login dependencies, so login-only routes are covered. A security-only admin is default-deny on writes outside `SECURITY_ADMIN_WRITES`, so a new route starts closed to them.
- **LlamaBot, by route:** `security_admin_required`, `system_admin_required`. `admin_required` now means owner only, so anything unclassified fails closed.
- **LlamaBot, tools:** a security-only admin has read-only tools on any box. Standard users are read-only under ZDR (LP-33).
- **Rails:** `AdminUserPolicy` and `AdminDashboardPolicy` (Pundit), and `LlamaBotRails::AdministrativeRoles` for grant rules.
- **ZDR boxes:** Rails `require_authentication?` is always true, because the open default would leave the audit log reachable by anyone.

Under ZDR, engineers lose system routes. On a normal box they keep them.

## Rules that close a way around the separation

- A security admin can impersonate only standard users, and cannot set an administrator's password. Either would hand them that person's access.
- Nobody but an owner changes their own roles, changes an owner, or makes an owner.
- Adding someone's second role counts as giving both.

## Audit

- LlamaBot: `AuditEvent`, read through `GET /api/audit-events`. The application cannot update or delete rows, but someone with database access can.
- Rails: `LlamaBotRails::ActivityEvent`, types `role_changed`, `access_denied`, `impersonation_started`. **Not append-only.**

## Known gaps

- **Two sets of accounts.** Rails and LlamaBot hold separate users, so a role is granted on each side. Same rule, not one record. Rails passes `security_only` in its chat token; Unified Login sets roles only when the account is created.
- **System admin audit view:** the ticket asks for read-only system events with security events redacted. Events have no such classification yet, so a system admin has no access.
- **Routes in the ticket that do not exist:** `/security`, `/sso`, `/roles`, `/deploys`, `/users/:id/reset_2fa`. Whoever builds them needs to add a policy. Rails has no system settings screens, so "security admin refused system settings" is proven in LlamaBot.
- **Role modes have no UI for a system admin:** `PUT /api/role-modes` works, but the form is on `/users`, which they cannot open.
- **Existing boxes:** the Rails admin controllers live in Leonardo's overlay, off `bin/update`'s allowlist, so only new boxes get them.
