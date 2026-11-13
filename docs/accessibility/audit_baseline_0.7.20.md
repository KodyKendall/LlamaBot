# Accessibility audit baseline, 0.7.20 (LP-34)

Audit for the University of Utah HECVAT: ITAC-06, ITAC-08 and ITAC-17. Run 2026-11-06 on llamapress-dev. The conformance report built from it is [VPAT_2.5_WCAG_draft_0.7.20.md](VPAT_2.5_WCAG_draft_0.7.20.md).

## How to re-run

```bash
cd ~/dev/Leonardo/e2e && npm run test:a11y
E2E_A11Y_REPORT=/tmp/axe.jsonl npm run test:a11y   # also logs every violation
```

The gate is `tests/accessibility.spec.ts` (axe) plus `tests/keyboard.spec.ts` (keyboard flows axe cannot see). CI runs both in the `e2e-smoke` job and the nightly canary. Any critical or serious violation fails the build.

## Screens in the ticket that do not exist

These were not audited because there is nothing to audit. Whoever builds them needs to add them to the gate.

| Ticket item | What is in the code |
| --- | --- |
| 2FA setup and challenge | Skeleton model only (`TwoFactorAuthenticatableUser`). No view or route in any repo, and Leonardo's `User` does not include it. |
| Passkey prompts | Skeleton model and `/.well-known/webauthn` only. No view. |
| Admin roles | No roles screen. There is one `admin` boolean. |
| Audit log | Exists as `/llama_bot/activity`. Audited. |
| Feedback "rating selector" | The bubble has none. The chat's good/bad session feedback uses native buttons. |

## Keyboard findings before fixes

| Area | Problem | WCAG | Fix |
| --- | --- | --- | --- |
| All pages | No skip link | 2.4.1 | Gem injects one on Rails pages; added to all LlamaBot pages |
| All pages | Focus outline removed or faint | 2.4.7 | 2px solid indigo ring, 2px offset, everywhere |
| Chat | Mode menu options were `<div>`s, unreachable | 2.1.1, 4.1.2 | `a11y.js`: focusable, Enter/Space, arrow keys, menu roles |
| Chat | Browser tabs (App, Inbox...) were `<div>`s | 2.1.1 | Focusable, `aria-current` on the active one |
| Chat | Conversation rows were `<div>`s | 2.1.1 | Focusable with Enter/Space |
| Chat | Closed drawer's rows stayed in the Tab order off-screen | 2.4.3, 2.4.7 | `visibility: hidden` while closed |
| Chat | Modals did not trap Tab or return focus; several ignored Escape | 2.1.2, 2.4.3 | One layer in `a11y.js` covers all of them |
| Chat | Menus had no `aria-expanded` and no Escape | 4.1.2 | Added |
| Chat | Panel divider was drag-only | 2.1.1 | Arrow keys on a focusable separator |
| Chat | Send and Stop are one button with one name | 4.1.2 | Name follows the action |
| Chat | Agent status and connection loss were silent | 4.1.3 | Polite live region; announcing does not move focus |
| Settings | Toggles hide the checkbox, so focus was invisible | 2.4.7 | Ring drawn on the switch |
| Feedback bubble | No name, no `aria-expanded`, no trap, no Escape, no focus return | 2.1.1, 2.1.2, 2.4.3, 4.1.2 | Fixed |
| Feedback bubble | Paperclip's file input was `display: none` | 2.1.1 | Kept reachable, visually hidden |
| Rails screenshot modal | No Escape, no trap | 2.1.2, 2.4.3 | Fixed. **No automated test yet.** |
| Forms | Errors were not announced or tied to fields | 3.3.1, 4.1.3 | `role="alert"`; `aria-invalid` and `aria-describedby` on invalid fields |

## Found after the first scan

Also fixed, and now in the gate unless noted: `/users` (labels, 9 elements, verified one-off because the e2e user is not an admin here), the new-prompt and new-job modals (labels, contrast), `/register` (verified one-off; the gate scans it on fresh instances, which CI is).

`/conversations` returns 500 because `conversations.html` does not exist. It is a dead route, left alone and out of the gate.

## Known gaps

**Pointer-only functions.** Each has another way to reach the same goal, but none works by keyboard, so ITAC-17 cannot be an unqualified Yes:

- Element selector (click an element in the preview). Alternative: describe it in the message.
- Eyedropper. Alternative: type the color.
- Screenshot region drag and annotation drawing. WCAG 2.1.1 exempts path-dependent drawing; the Cancel and Attach buttons do work by keyboard.
- Feedback kanban drag, if it has no other way to change status. Not checked.

**VS Code tab.** Third-party code-server. It keeps Tab inside the editor unless the user turns on Tab focus mode (Ctrl+M).

**No screen-reader test.** Names, roles and live regions are asserted in the DOM. Nobody has used this with NVDA, JAWS or VoiceOver.

**Chat modals.** The shared layer is tested on the asset and prompt modals. The other modals (GitHub sign-in, update, discard warning, diff viewer, slash commands and others) rely on the same code but were not opened individually.

**Not covered by axe at all:** non-text contrast (1.4.11), reflow (1.4.10), text spacing (1.4.12), hover content (1.4.13). See the VPAT.

**Customer apps.** Leo-generated pages get the injected baseline only. Their conformance is not claimed.

## Screens outside the baseline, not fixed

Scanned once for information on 2026-11-06. **Not in the gate.** These need the same treatment before any claim that covers the whole product.

| Screen | Issues (elements) |
| --- | --- |
| `/` | serious color-contrast (1) |
| `/home` | serious color-contrast (1) |
| `/users` | serious color-contrast (1) |
| `/llama_bot/tickets` | serious color-contrast (6), critical select-name (3) |
| `/llama_bot/tickets/timeline` | critical button-name (1), serious color-contrast (2), critical label (1), critical select-name (3) |
| `/llama_bot/feedback/kanban` | serious color-contrast (6) |
| `/llama_bot/feedback/dashboard` | serious color-contrast (6) |
| `/llama_bot/notifications` | serious color-contrast (1) |
| `/llama_bot/conversations` | serious color-contrast (2), critical select-name (1) |
| `/llama_bot/requests` | critical select-name (2) |
| `/llama_bot/projects` | serious color-contrast (1) |

## Summary

253 failing elements across 25 scanned screens and states: 31 critical, 222 serious, 0 moderate, 0 minor. All were found with axe-core 4.10 before any fix, using the WCAG 2.0 and 2.1 A and AA rule tags.

## Automated findings before fixes

Every row was fixed in 0.7.20. The current run of `npm run test:a11y` is the proof.

### llamabot /login

| Impact | Rule | WCAG | Element |
| --- | --- | --- | --- |
| serious | color-contrast | 1.4.3 | `span` |
| serious | html-has-lang | 3.1.1 | `html` |

### chat

| Impact | Rule | WCAG | Element |
| --- | --- | --- | --- |
| critical | button-name | 4.1.2 | `.hamburger-menu` |
| serious | link-name | 2.4.4, 4.1.2 | `.backup-status` |

### chat: conversations drawer

| Impact | Rule | WCAG | Element |
| --- | --- | --- | --- |
| critical | button-name | 4.1.2 | `.hamburger-menu` |
| serious | color-contrast | 1.4.3 | `.menu-item:nth-child(1) > .thread-timestamp` |
| serious | color-contrast | 1.4.3 | `.menu-item:nth-child(2) > .thread-timestamp` |
| serious | color-contrast | 1.4.3 | `.menu-item:nth-child(3) > .thread-timestamp` |
| serious | color-contrast | 1.4.3 | `.menu-item:nth-child(4) > .thread-timestamp` |
| serious | color-contrast | 1.4.3 | `.menu-item:nth-child(5) > .thread-timestamp` |
| serious | link-name | 2.4.4, 4.1.2 | `.backup-status` |

### chat: mode menu

| Impact | Rule | WCAG | Element |
| --- | --- | --- | --- |
| critical | button-name | 4.1.2 | `.hamburger-menu` |
| serious | color-contrast | 1.4.3 | `div[data-mode="auto"] > .execution-mode-option-desc` |
| serious | color-contrast | 1.4.3 | `div[data-mode="plan"] > .execution-mode-option-desc` |
| serious | link-name | 2.4.4, 4.1.2 | `.backup-status` |

### chat: attach menu

| Impact | Rule | WCAG | Element |
| --- | --- | --- | --- |
| critical | button-name | 4.1.2 | `.hamburger-menu` |
| serious | link-name | 2.4.4, 4.1.2 | `.backup-status` |

### chat: tools toolbar

| Impact | Rule | WCAG | Element |
| --- | --- | --- | --- |
| critical | button-name | 4.1.2 | `.hamburger-menu` |
| serious | link-name | 2.4.4, 4.1.2 | `.backup-status` |
| critical | select-name | 4.1.2 | `select[data-llamabot="agent-mode-select"]` |

### chat: version history

| Impact | Rule | WCAG | Element |
| --- | --- | --- | --- |
| critical | button-name | 4.1.2 | `.hamburger-menu` |
| serious | color-contrast | 1.4.3 | `.btn-discard-changes` |
| serious | color-contrast | 1.4.3 | `div[data-commit-index="0"] > .checkpoint-content > .git-commit-refs-row > .git-ref-badge` |
| serious | color-contrast | 1.4.3 | `div[data-commit-index="0"] > .checkpoint-content > .checkpoint-meta > .git-commit-sha-badge[title="Click to co` |
| serious | color-contrast | 1.4.3 | `.current-checkpoint > .checkpoint-content > .git-commit-refs-row > .git-ref-badge:nth-child(2)` |
| serious | color-contrast | 1.4.3 | `.git-ref-badge:nth-child(3)` |
| serious | color-contrast | 1.4.3 | `.current-checkpoint > .checkpoint-content > .checkpoint-meta > .git-commit-sha-badge[title="Click to copy"]` |
| serious | color-contrast | 1.4.3 | `div[data-commit-index="2"] > .checkpoint-content > .checkpoint-meta > .git-commit-sha-badge[title="Click to co` |
| serious | color-contrast | 1.4.3 | `div[data-commit-index="3"] > .checkpoint-content > .git-commit-refs-row > .git-ref-badge:nth-child(1)` |
| serious | color-contrast | 1.4.3 | `div[data-commit-index="3"] > .checkpoint-content > .git-commit-refs-row > .git-ref-badge:nth-child(2)` |
| serious | color-contrast | 1.4.3 | `div[data-commit-index="3"] > .checkpoint-content > .checkpoint-meta > .git-commit-sha-badge[title="Click to co` |
| serious | link-name | 2.4.4, 4.1.2 | `.backup-status` |
| serious | scrollable-region-focusable | 2.1.1, 2.1.3 | `.checkpoint-panel-content` |

### llamabot /settings

| Impact | Rule | WCAG | Element |
| --- | --- | --- | --- |
| serious | color-contrast | 1.4.3 | `#chatgptAccountNote` |
| serious | color-contrast | 1.4.3 | `.card:nth-child(8) > div:nth-child(4)` |
| serious | color-contrast | 1.4.3 | `.card:nth-child(9) > div:nth-child(3)` |
| serious | color-contrast | 1.4.3 | `.card:nth-child(10) > div:nth-child(3)` |
| serious | color-contrast | 1.4.3 | `#voiceDictationNote` |
| serious | color-contrast | 1.4.3 | `.card:nth-child(12) > div:nth-child(2)` |
| serious | color-contrast | 1.4.3 | `#vscodeStatus` |
| serious | color-contrast | 1.4.3 | `.card:nth-child(13) > div:nth-child(2)` |
| serious | color-contrast | 1.4.3 | `.card:nth-child(13) > div:nth-child(4)` |
| serious | color-contrast | 1.4.3 | `.card:nth-child(13) > div:nth-child(6)` |
| serious | color-contrast | 1.4.3 | `.card:nth-child(13) > div:nth-child(8)` |
| serious | color-contrast | 1.4.3 | `.card:nth-child(14) > div:nth-child(3)` |
| serious | color-contrast | 1.4.3 | `.card:nth-child(15) > div:nth-child(3)` |
| serious | color-contrast | 1.4.3 | `.card:nth-child(15) > div:nth-child(5)` |
| serious | color-contrast | 1.4.3 | `button` |
| serious | html-has-lang | 3.1.1 | `html` |
| critical | label | 4.1.2 | `#autoBackupToggle` |
| critical | label | 4.1.2 | `#autoRefreshOnEditToggle` |
| critical | label | 4.1.2 | `#tokenWheelToggle` |
| critical | label | 4.1.2 | `#voiceDictationToggle` |
| critical | label | 4.1.2 | `#vscodeToggle` |
| critical | label | 4.1.2 | `label[title="The App tab is always visible"] > input` |
| critical | label | 4.1.2 | `input[data-target="inboxFrame"]` |
| critical | label | 4.1.2 | `input[data-target="activityFrame"]` |
| critical | label | 4.1.2 | `#proactiveBuildToggle` |
| critical | label | 4.1.2 | `#browserInspectToggle` |
| critical | label | 4.1.2 | `#liveBrowserToolsToggle` |
| serious | link-name | 2.4.4, 4.1.2 | `.back-btn` |

### llamabot /prompt-library

| Impact | Rule | WCAG | Element |
| --- | --- | --- | --- |
| serious | color-contrast | 1.4.3 | `div[onclick="editPrompt(1)"] > .prompt-meta > .prompt-group-badge` |
| serious | color-contrast | 1.4.3 | `div[onclick="editPrompt(1)"] > .prompt-meta > span:nth-child(2)` |
| serious | color-contrast | 1.4.3 | `div[onclick="editPrompt(2)"] > .prompt-meta > .prompt-group-badge` |
| serious | color-contrast | 1.4.3 | `div[onclick="editPrompt(2)"] > .prompt-meta > span:nth-child(2)` |
| serious | html-has-lang | 3.1.1 | `html` |
| serious | link-name | 2.4.4, 4.1.2 | `a` |
| critical | select-name | 4.1.2 | `#promptGroupFilter` |

### llamabot /git-history

| Impact | Rule | WCAG | Element |
| --- | --- | --- | --- |
| serious | color-contrast | 1.4.3 | `.commit-row:nth-child(1) > .commit-info > .commit-meta > .commit-sha` |
| serious | color-contrast | 1.4.3 | `.current > .commit-info > .commit-meta > .commit-sha` |
| serious | color-contrast | 1.4.3 | `.current > .commit-info > .commit-meta > .commit-refs > .head.ref-badge` |
| serious | color-contrast | 1.4.3 | `.commit-row:nth-child(3) > .commit-info > .commit-meta > .commit-sha` |
| serious | color-contrast | 1.4.3 | `.commit-row:nth-child(4) > .commit-info > .commit-meta > .commit-sha` |
| serious | color-contrast | 1.4.3 | `.commit-row:nth-child(4) > .commit-info > .commit-meta > .commit-refs > .head.ref-badge` |
| serious | color-contrast | 1.4.3 | `.commit-row:nth-child(5) > .commit-info > .commit-meta > .commit-sha` |
| serious | color-contrast | 1.4.3 | `.commit-row:nth-child(6) > .commit-info > .commit-meta > .commit-sha` |
| serious | color-contrast | 1.4.3 | `.commit-row:nth-child(7) > .commit-info > .commit-meta > .commit-sha` |
| serious | color-contrast | 1.4.3 | `.commit-row:nth-child(8) > .commit-info > .commit-meta > .commit-sha` |
| serious | color-contrast | 1.4.3 | `.commit-row:nth-child(9) > .commit-info > .commit-meta > .commit-sha` |
| serious | color-contrast | 1.4.3 | `.commit-row:nth-child(10) > .commit-info > .commit-meta > .commit-sha` |
| serious | color-contrast | 1.4.3 | `.commit-row:nth-child(11) > .commit-info > .commit-meta > .commit-sha` |
| serious | html-has-lang | 3.1.1 | `html` |
| serious | link-name | 2.4.4, 4.1.2 | `a` |
| serious | scrollable-region-focusable | 2.1.1, 2.1.3 | `#legendList` |
| serious | scrollable-region-focusable | 2.1.1, 2.1.3 | `#graphContainer` |
| critical | select-name | 4.1.2 | `#branchFilter` |

### llamabot /scheduled-jobs

| Impact | Rule | WCAG | Element |
| --- | --- | --- | --- |
| serious | color-contrast | 1.4.3 | `button[onclick="showTab('jobs')"]` |
| serious | html-has-lang | 3.1.1 | `html` |
| serious | link-name | 2.4.4, 4.1.2 | `a` |

### llamabot /backup-history

| Impact | Rule | WCAG | Element |
| --- | --- | --- | --- |
| serious | html-has-lang | 3.1.1 | `html` |

### llamabot /leonardo-md

| Impact | Rule | WCAG | Element |
| --- | --- | --- | --- |
| serious | color-contrast | 1.4.3 | `span` |
| serious | html-has-lang | 3.1.1 | `html` |
| serious | link-name | 2.4.4, 4.1.2 | `a` |

### llamabot /settings/environment

| Impact | Rule | WCAG | Element |
| --- | --- | --- | --- |
| serious | color-contrast | 1.4.3 | `.card:nth-child(3) > .card-header` |
| serious | color-contrast | 1.4.3 | `.row:nth-child(1) > .row-main > .row-desc` |
| serious | color-contrast | 1.4.3 | `.row:nth-child(2) > .row-main > .row-desc` |
| serious | color-contrast | 1.4.3 | `.card:nth-child(4) > .card-header` |
| serious | color-contrast | 1.4.3 | `.card:nth-child(4) > .card-note` |
| serious | color-contrast | 1.4.3 | `.group-title:nth-child(1) > span:nth-child(1)` |
| serious | color-contrast | 1.4.3 | `.group-title:nth-child(6) > span:nth-child(1)` |
| serious | color-contrast | 1.4.3 | `.group-title:nth-child(6) > .badge-count.badge` |
| serious | color-contrast | 1.4.3 | `.off.model-row:nth-child(7) > .model-name > div:nth-child(1)` |
| serious | color-contrast | 1.4.3 | `.off.model-row:nth-child(7) > .model-name > .model-reason` |
| serious | color-contrast | 1.4.3 | `.off.model-row:nth-child(8) > .model-name > div:nth-child(1)` |
| serious | color-contrast | 1.4.3 | `.off.model-row:nth-child(8) > .model-name > .model-reason` |
| serious | color-contrast | 1.4.3 | `.off.model-row:nth-child(9) > .model-name > div:nth-child(1)` |
| serious | color-contrast | 1.4.3 | `.off.model-row:nth-child(9) > .model-name > .model-reason` |
| serious | color-contrast | 1.4.3 | `.off.model-row:nth-child(10) > .model-name > div:nth-child(1)` |
| serious | color-contrast | 1.4.3 | `.off.model-row:nth-child(10) > .model-name > .model-reason` |
| serious | color-contrast | 1.4.3 | `.off.model-row:nth-child(11) > .model-name > div:nth-child(1)` |
| serious | color-contrast | 1.4.3 | `.off.model-row:nth-child(11) > .model-name > .model-reason` |
| serious | color-contrast | 1.4.3 | `.off.model-row:nth-child(12) > .model-name > div:nth-child(1)` |
| serious | color-contrast | 1.4.3 | `.off.model-row:nth-child(12) > .model-name > .model-reason` |
| serious | color-contrast | 1.4.3 | `.off.model-row:nth-child(13) > .model-name > div:nth-child(1)` |
| serious | color-contrast | 1.4.3 | `.off.model-row:nth-child(13) > .model-name > .model-reason` |
| serious | color-contrast | 1.4.3 | `.off.model-row:nth-child(14) > .model-name > div:nth-child(1)` |
| serious | color-contrast | 1.4.3 | `.off.model-row:nth-child(14) > .model-name > .model-reason` |
| serious | color-contrast | 1.4.3 | `.off.model-row:nth-child(15) > .model-name > div:nth-child(1)` |
| serious | color-contrast | 1.4.3 | `.off.model-row:nth-child(15) > .model-name > .model-reason` |
| serious | color-contrast | 1.4.3 | `.off.model-row:nth-child(16) > .model-name > div:nth-child(1)` |
| serious | color-contrast | 1.4.3 | `.off.model-row:nth-child(16) > .model-name > .model-reason` |
| serious | color-contrast | 1.4.3 | `.off.model-row:nth-child(17) > .model-name > div:nth-child(1)` |
| serious | color-contrast | 1.4.3 | `.off.model-row:nth-child(17) > .model-name > .model-reason` |
| serious | color-contrast | 1.4.3 | `.off.model-row:nth-child(18) > .model-name > div:nth-child(1)` |
| serious | color-contrast | 1.4.3 | `.off.model-row:nth-child(18) > .model-name > .model-reason` |
| serious | color-contrast | 1.4.3 | `.off.model-row:nth-child(19) > .model-name > div:nth-child(1)` |
| serious | color-contrast | 1.4.3 | `.off.model-row:nth-child(19) > .model-name > .model-reason` |
| serious | color-contrast | 1.4.3 | `.off.model-row:nth-child(20) > .model-name > div:nth-child(1)` |
| serious | color-contrast | 1.4.3 | `.off.model-row:nth-child(20) > .model-name > .model-reason` |
| serious | color-contrast | 1.4.3 | `.off.model-row:nth-child(21) > .model-name > div:nth-child(1)` |
| serious | color-contrast | 1.4.3 | `.off.model-row:nth-child(21) > .model-name > .model-reason` |
| serious | color-contrast | 1.4.3 | `.off.model-row:nth-child(22) > .model-name > div:nth-child(1)` |
| serious | color-contrast | 1.4.3 | `.off.model-row:nth-child(22) > .model-name > .model-reason` |
| serious | color-contrast | 1.4.3 | `.off.model-row:nth-child(23) > .model-name > div:nth-child(1)` |
| serious | color-contrast | 1.4.3 | `.off.model-row:nth-child(23) > .model-name > .model-reason` |
| serious | color-contrast | 1.4.3 | `.off.model-row:nth-child(24) > .model-name > div:nth-child(1)` |
| serious | color-contrast | 1.4.3 | `.off.model-row:nth-child(24) > .model-name > .model-reason` |
| serious | color-contrast | 1.4.3 | `.off.model-row:nth-child(25) > .model-name > div:nth-child(1)` |
| serious | color-contrast | 1.4.3 | `.off.model-row:nth-child(25) > .model-name > .model-reason` |
| serious | color-contrast | 1.4.3 | `.off.model-row:nth-child(26) > .model-name > div:nth-child(1)` |
| serious | color-contrast | 1.4.3 | `.off.model-row:nth-child(26) > .model-name > .model-reason` |
| serious | color-contrast | 1.4.3 | `.off.model-row:nth-child(27) > .model-name > div:nth-child(1)` |
| serious | color-contrast | 1.4.3 | `.off.model-row:nth-child(27) > .model-name > .model-reason` |
| serious | color-contrast | 1.4.3 | `.off.model-row:nth-child(28) > .model-name > div:nth-child(1)` |
| serious | color-contrast | 1.4.3 | `.off.model-row:nth-child(28) > .model-name > .model-reason` |
| serious | color-contrast | 1.4.3 | `.off.model-row:nth-child(29) > .model-name > div:nth-child(1)` |
| serious | color-contrast | 1.4.3 | `.off.model-row:nth-child(29) > .model-name > .model-reason` |
| serious | color-contrast | 1.4.3 | `.off.model-row:nth-child(30) > .model-name > div:nth-child(1)` |
| serious | color-contrast | 1.4.3 | `.off.model-row:nth-child(30) > .model-name > .model-reason` |
| serious | color-contrast | 1.4.3 | `.off.model-row:nth-child(31) > .model-name > div:nth-child(1)` |
| serious | color-contrast | 1.4.3 | `.off.model-row:nth-child(31) > .model-name > .model-reason` |
| serious | color-contrast | 1.4.3 | `.off.model-row:nth-child(32) > .model-name > div:nth-child(1)` |
| serious | color-contrast | 1.4.3 | `.off.model-row:nth-child(32) > .model-name > .model-reason` |
| serious | color-contrast | 1.4.3 | `.off.model-row:nth-child(33) > .model-name > div:nth-child(1)` |
| serious | color-contrast | 1.4.3 | `.off.model-row:nth-child(33) > .model-name > .model-reason` |
| serious | color-contrast | 1.4.3 | `.group-title:nth-child(34) > span:nth-child(1)` |
| serious | color-contrast | 1.4.3 | `.off.model-row:nth-child(35) > .model-name > div:nth-child(1)` |
| serious | color-contrast | 1.4.3 | `.off.model-row:nth-child(35) > .model-name > .model-reason` |
| serious | color-contrast | 1.4.3 | `.off.model-row:nth-child(36) > .model-name > div:nth-child(1)` |
| serious | color-contrast | 1.4.3 | `.off.model-row:nth-child(36) > .model-name > .model-reason` |
| serious | color-contrast | 1.4.3 | `.card:nth-child(5) > .card-header` |
| serious | color-contrast | 1.4.3 | `.card:nth-child(5) > .card-note` |
| serious | color-contrast | 1.4.3 | `.locked-note > div` |
| serious | html-has-lang | 3.1.1 | `html` |
| critical | label | 4.1.2 | `.row:nth-child(1) > label > input[type="checkbox"]` |
| critical | label | 4.1.2 | `.row:nth-child(2) > label > input[type="checkbox"]` |
| serious | link-name | 2.4.4, 4.1.2 | `a` |

### rails /users/sign_in

| Impact | Rule | WCAG | Element |
| --- | --- | --- | --- |
| serious | color-contrast | 1.4.3 | `.mx-3` |
| serious | html-has-lang | 3.1.1 | `html` |

### rails /users/sign_up

| Impact | Rule | WCAG | Element |
| --- | --- | --- | --- |
| serious | html-has-lang | 3.1.1 | `html` |

### rails /users/password/new

| Impact | Rule | WCAG | Element |
| --- | --- | --- | --- |
| serious | html-has-lang | 3.1.1 | `html` |

### rails /users/edit

| Impact | Rule | WCAG | Element |
| --- | --- | --- | --- |
| serious | html-has-lang | 3.1.1 | `html` |

### rails /admin

| Impact | Rule | WCAG | Element |
| --- | --- | --- | --- |
| serious | html-has-lang | 3.1.1 | `html` |

### rails /admin/users

| Impact | Rule | WCAG | Element |
| --- | --- | --- | --- |
| serious | color-contrast | 1.4.3 | `.bg-blue-500` |
| serious | color-contrast | 1.4.3 | `form[action="/admin/users/9/impersonate"] > .text-blue-500[type="submit"]` |
| serious | color-contrast | 1.4.3 | `form[action="/admin/users/7/impersonate"] > .text-blue-500[type="submit"]` |
| serious | color-contrast | 1.4.3 | `form[action="/admin/users/12/impersonate"] > .text-blue-500[type="submit"]` |
| serious | html-has-lang | 3.1.1 | `html` |
| critical | label | 4.1.2 | `#q` |

### rails /llama_bot/activity

| Impact | Rule | WCAG | Element |
| --- | --- | --- | --- |
| serious | color-contrast | 1.4.3 | `.mt-1` |
| serious | color-contrast | 1.4.3 | `.max-w-5xl > p` |
| serious | color-contrast | 1.4.3 | `p > .font-medium` |
| serious | color-contrast | 1.4.3 | `.mb-6:nth-child(4) > h2` |
| serious | color-contrast | 1.4.3 | `div[data-activity-event="51"] > .w-20.tabular-nums.text-gray-400` |
| serious | color-contrast | 1.4.3 | `div[data-activity-event="50"] > .w-20.tabular-nums.text-gray-400` |
| serious | color-contrast | 1.4.3 | `div[data-activity-event="49"] > .w-20.tabular-nums.text-gray-400` |
| serious | color-contrast | 1.4.3 | `div[data-activity-event="48"] > .w-20.tabular-nums.text-gray-400` |
| serious | color-contrast | 1.4.3 | `div[data-activity-event="47"] > .w-20.tabular-nums.text-gray-400` |
| serious | color-contrast | 1.4.3 | `div[data-activity-event="46"] > .w-20.tabular-nums.text-gray-400` |
| serious | color-contrast | 1.4.3 | `div[data-activity-event="45"] > .w-20.tabular-nums.text-gray-400` |
| serious | color-contrast | 1.4.3 | `div[data-activity-event="44"] > .w-20.tabular-nums.text-gray-400` |
| serious | color-contrast | 1.4.3 | `div[data-activity-event="43"] > .w-20.tabular-nums.text-gray-400` |
| serious | color-contrast | 1.4.3 | `div[data-activity-event="42"] > .w-20.tabular-nums.text-gray-400` |
| serious | color-contrast | 1.4.3 | `div[data-activity-event="41"] > .w-20.tabular-nums.text-gray-400` |
| serious | color-contrast | 1.4.3 | `.mb-6:nth-child(5) > h2` |
| serious | color-contrast | 1.4.3 | `div[data-activity-event="40"] > .w-20.tabular-nums.text-gray-400` |
| serious | color-contrast | 1.4.3 | `div[data-activity-event="39"] > .w-20.tabular-nums.text-gray-400` |
| serious | color-contrast | 1.4.3 | `div[data-activity-event="38"] > .w-20.tabular-nums.text-gray-400` |
| serious | color-contrast | 1.4.3 | `div[data-activity-event="37"] > .w-20.tabular-nums.text-gray-400` |
| serious | color-contrast | 1.4.3 | `div[data-activity-event="36"] > .w-20.tabular-nums.text-gray-400` |
| serious | color-contrast | 1.4.3 | `div[data-activity-event="35"] > .w-20.tabular-nums.text-gray-400` |
| serious | color-contrast | 1.4.3 | `div[data-activity-event="34"] > .w-20.tabular-nums.text-gray-400` |
| serious | color-contrast | 1.4.3 | `div[data-activity-event="33"] > .w-20.tabular-nums.text-gray-400` |
| serious | color-contrast | 1.4.3 | `div[data-activity-event="32"] > .w-20.tabular-nums.text-gray-400` |
| serious | color-contrast | 1.4.3 | `div[data-activity-event="31"] > .w-20.tabular-nums.text-gray-400` |
| serious | color-contrast | 1.4.3 | `.mb-6:nth-child(6) > h2` |
| serious | color-contrast | 1.4.3 | `div[data-activity-event="30"] > .w-20.tabular-nums.text-gray-400` |
| serious | color-contrast | 1.4.3 | `div[data-activity-event="29"] > .w-20.tabular-nums.text-gray-400` |
| serious | color-contrast | 1.4.3 | `div[data-activity-event="28"] > .w-20.tabular-nums.text-gray-400` |
| serious | color-contrast | 1.4.3 | `div[data-activity-event="27"] > .w-20.tabular-nums.text-gray-400` |
| serious | color-contrast | 1.4.3 | `.justify-between.text-sm.text-gray-500 > span` |
| serious | color-contrast | 1.4.3 | `.btn-disabled` |
| serious | html-has-lang | 3.1.1 | `html` |
| critical | label | 4.1.2 | `#from` |
| critical | label | 4.1.2 | `#to` |
| critical | select-name | 4.1.2 | `#actor_id` |
| critical | select-name | 4.1.2 | `#event_type` |
| critical | select-name | 4.1.2 | `#source` |
| critical | select-name | 4.1.2 | `#category` |

### rails /llama_bot/activity/usage

| Impact | Rule | WCAG | Element |
| --- | --- | --- | --- |
| serious | color-contrast | 1.4.3 | `.text-gray-500` |
| serious | color-contrast | 1.4.3 | `.mb-6:nth-child(2)` |
| serious | color-contrast | 1.4.3 | `.mb-6:nth-child(2) > span` |
| serious | color-contrast | 1.4.3 | `.grid-cols-2 > .card.bg-base-100.shadow-sm:nth-child(1) > .card-body.py-4 > .uppercase.tracking-wide.text-xs` |
| serious | color-contrast | 1.4.3 | `.card.bg-base-100.shadow-sm:nth-child(1) > .card-body.py-4 > .text-xs.text-gray-400:nth-child(3)` |
| serious | color-contrast | 1.4.3 | `.grid-cols-2 > .card.bg-base-100.shadow-sm:nth-child(2) > .card-body.py-4 > .uppercase.tracking-wide.text-xs` |
| serious | color-contrast | 1.4.3 | `.card.bg-base-100.shadow-sm:nth-child(2) > .card-body.py-4 > .text-xs.text-gray-400:nth-child(3)` |
| serious | color-contrast | 1.4.3 | `.grid-cols-2 > .card.bg-base-100.shadow-sm:nth-child(3) > .card-body.py-4 > .uppercase.tracking-wide.text-xs` |
| serious | color-contrast | 1.4.3 | `.card.bg-base-100.shadow-sm:nth-child(4) > .card-body.py-4 > .uppercase.tracking-wide.text-xs` |
| serious | color-contrast | 1.4.3 | `.md\:grid-cols-3 > .card.bg-base-100.shadow-sm:nth-child(1) > .card-body.py-4 > .uppercase.tracking-wide.text-` |
| serious | color-contrast | 1.4.3 | `.md\:grid-cols-3 > .card.bg-base-100.shadow-sm:nth-child(2) > .card-body.py-4 > .uppercase.tracking-wide.text-` |
| serious | color-contrast | 1.4.3 | `.md\:grid-cols-3 > .card.bg-base-100.shadow-sm:nth-child(3) > .card-body.py-4 > .uppercase.tracking-wide.text-` |
| serious | color-contrast | 1.4.3 | `.text-error` |
| serious | color-contrast | 1.4.3 | `.mb-8:nth-child(5)` |
| serious | color-contrast | 1.4.3 | `div:nth-child(1) > h2` |
| serious | color-contrast | 1.4.3 | `.card-body.py-4 > p` |
| serious | color-contrast | 1.4.3 | `div:nth-child(2) > h2` |
| serious | color-contrast | 1.4.3 | `.py-2.border-b.border-base-200:nth-child(1) > .min-w-0 > .text-xs.text-gray-400` |
| serious | color-contrast | 1.4.3 | `.py-2.border-b.border-base-200:nth-child(1) > .text-right.shrink-0.ml-3 > .text-xs.text-gray-400` |
| serious | color-contrast | 1.4.3 | `.py-2.border-b.border-base-200:nth-child(2) > .min-w-0 > .text-xs.text-gray-400` |
| serious | color-contrast | 1.4.3 | `.py-2.border-b.border-base-200:nth-child(2) > .text-right.shrink-0.ml-3 > .text-xs.text-gray-400` |
| serious | color-contrast | 1.4.3 | `.py-2.border-b.border-base-200:nth-child(3) > .min-w-0 > .text-xs.text-gray-400` |
| serious | color-contrast | 1.4.3 | `.py-2.border-b.border-base-200:nth-child(3) > .text-right.shrink-0.ml-3 > .text-xs.text-gray-400` |
| serious | html-has-lang | 3.1.1 | `html` |

### rails /llama_bot/feedback

| Impact | Rule | WCAG | Element |
| --- | --- | --- | --- |
| serious | color-contrast | 1.4.3 | `.collapse.collapse-arrow.mt-8:nth-child(3) > .collapse-title.py-4 > .gap-3.items-center.flex > div:nth-child(2` |
| serious | color-contrast | 1.4.3 | `.collapse.collapse-arrow.mt-8:nth-child(4) > .collapse-title.py-4 > .gap-3.items-center.flex > div:nth-child(2` |
| serious | html-has-lang | 3.1.1 | `html` |
| critical | label | 4.1.2 | `.collapse.collapse-arrow.mt-8:nth-child(3) > input[type="checkbox"]` |
| critical | label | 4.1.2 | `.collapse.collapse-arrow.mt-8:nth-child(4) > input[type="checkbox"]` |

### rails /llama_bot/feedback/new

| Impact | Rule | WCAG | Element |
| --- | --- | --- | --- |
| serious | color-contrast | 1.4.3 | `.mt-1` |
| serious | html-has-lang | 3.1.1 | `html` |

### feedback bubble

| Impact | Rule | WCAG | Element |
| --- | --- | --- | --- |
| serious | color-contrast | 1.4.3 | `#feedback-version-chip > span` |
| serious | color-contrast | 1.4.3 | `.font-mono` |
| serious | html-has-lang | 3.1.1 | `html` |

