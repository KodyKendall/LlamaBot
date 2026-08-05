# Test plans

Manual QA checklists, one file per release (`0.6.0g.md`, `0.6.0h.md`, …).

These cover what automated tests **can't**: does the thing actually feel right in a real
browser, on a real box. Automated tests remain the gate for merging (see
`Leonardo/docs/TESTING.md`) — a plan here is never a substitute for a failing test first.

## How to use

1. When you ship a user-visible change, add a section to the current release's plan.
   Write the steps as a human would do them, not as the code works.
2. During QA, check the boxes off in a PR against this file. An unchecked box on a
   released version is a known gap, not an oversight to hide.
3. If a step fails, note what happened inline under the box and open a ticket. Don't
   delete the step.

## Writing a good section

- **One box = one observable outcome.** "The preview comes back on the same page" — not
  "verify persistence works".
- **Include the negative cases.** What should NOT happen, and what happens when the
  feature is unavailable (storage blocked, permission missing, network down).
- **Include the setup.** Which URL, which role, whether a rebuild is needed.
- **Say who it's for.** Some steps need a `user`-role account or a second box; call that
  out so the checker knows when to skip vs. when they're blocked.
