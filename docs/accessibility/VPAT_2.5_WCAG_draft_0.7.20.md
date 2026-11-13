# Accessibility Conformance Report: DRAFT

Follows the VPAT® 2.5 WCAG edition structure.

**Draft status: not ready to send.** It needs the named human review below, and the vendor contact and legal text are blank. Before submission, transfer it to ITI's official template.

| | |
| --- | --- |
| Product | Leonardo (LlamaPress), version 0.7.20 |
| Report date | 2026-11-06 |
| Standard | WCAG 2.1, Levels A and AA |
| Vendor contact | _To be completed_ |
| Evidence | [audit_baseline_0.7.20.md](audit_baseline_0.7.20.md) |

## Scope

**Covered:** LlamaBot sign-in, chat (send, stop, mode switch, file attach, conversation drawer, version history), settings, prompt library, git history, scheduled jobs, backup history, instructions page. Rails sign-in, sign-up, password reset, account settings, admin dashboard and users, activity log, usage, feedback list and form, feedback bubble.

**Not covered:**

- Other Rails inbox screens (tickets, kanban, dashboard, notifications, conversations, requests, projects). These have known unfixed issues, listed in the audit.
- 2FA and passkey screens, which do not exist yet.
- Customer applications generated with the product.
- The embedded third-party code editor.

## Evaluation methods

- Automated: axe-core 4.10 through Playwright in Chromium, run in CI. Zero critical or serious violations on the covered screens. This is a release gate.
- Keyboard: automated keyboard-only tests of the main flows (skip link, focus rings, focus trap, Escape, focus return).
- **Not done: testing with a screen reader, and testing by users with disabilities.** Every "Supports" below rests on the automated evidence and needs that confirmation.

## Terms

- **Supports:** works without known defects on the covered screens.
- **Partially Supports:** some functionality does not meet the criterion.
- **Does Not Support:** the majority of functionality does not meet it.
- **Not Applicable:** the criterion is not relevant to the product.
- **Not Evaluated:** no evidence gathered yet. VPAT 2.5 allows this only for AAA, so **each of these must be resolved before submission.**

## Table 1: WCAG 2.1 Level A

| Criterion | Conformance | Remarks |
| --- | --- | --- |
| 1.1.1 Non-text Content | Partially Supports | Controls and images flagged by axe are named. Decorative icons and AI-rendered content were not reviewed by hand. |
| 1.2.1 Audio-only and Video-only | Not Applicable | The product ships no prerecorded media. User-recorded clips are user content. |
| 1.2.2 Captions (Prerecorded) | Not Applicable | As 1.2.1. |
| 1.2.3 Audio Description or Media Alternative | Not Applicable | As 1.2.1. |
| 1.3.1 Info and Relationships | Partially Supports | Labels and landmarks fixed on covered screens. Heading order not reviewed by hand. |
| 1.3.2 Meaningful Sequence | Supports | Keyboard tests follow DOM order on the main flows. |
| 1.3.3 Sensory Characteristics | Not Evaluated | |
| 1.4.1 Use of Color | Not Evaluated | The connection indicator is a colored dot; its state is also announced. Other uses not reviewed. |
| 1.4.2 Audio Control | Not Evaluated | The chat plays short notification sounds. Their length has not been measured. |
| 2.1.1 Keyboard | Partially Supports | Main flows work by keyboard. Exceptions: element selector, eyedropper, screenshot region drag. Annotation drawing is path-dependent, which the criterion exempts. |
| 2.1.2 No Keyboard Trap | Partially Supports | Modals trap on purpose and release on Escape. The third-party code editor holds Tab until Tab focus mode is on (Ctrl+M). |
| 2.1.4 Character Key Shortcuts | Not Evaluated | |
| 2.2.1 Timing Adjustable | Not Evaluated | Sessions time out. A warning banner exists but is turned off by a config flag; not reviewed. |
| 2.2.2 Pause, Stop, Hide | Not Evaluated | Cycling status text and the building overlay animate. |
| 2.3.1 Three Flashes or Below | Not Evaluated | |
| 2.4.1 Bypass Blocks | Supports | Skip link on covered screens. On Rails it is added automatically unless a page has no `<main>` or `<h1>`. |
| 2.4.2 Page Titled | Partially Supports | Titles exist, but some are generic: "Rails Basic" on Rails pages. |
| 2.4.3 Focus Order | Supports | Tested on the main flows, including focus return after modals. |
| 2.4.4 Link Purpose (In Context) | Supports | Axe link-name passes on covered screens. |
| 2.5.1 Pointer Gestures | Not Evaluated | |
| 2.5.2 Pointer Cancellation | Not Evaluated | |
| 2.5.3 Label in Name | Not Evaluated | |
| 2.5.4 Motion Actuation | Not Applicable | No motion input. |
| 3.1.1 Language of Page | Supports | Set on every page. |
| 3.2.1 On Focus | Supports | Menus close when focus leaves them; nothing else changes on focus in the tested flows. |
| 3.2.2 On Input | Not Evaluated | |
| 3.3.1 Error Identification | Partially Supports | Sign-in, sign-up and password errors are announced. Other Rails forms get `aria-invalid` but rely on each form rendering an error summary. |
| 3.3.2 Labels or Instructions | Supports | Axe label rules pass on covered screens, including the modals scanned. |
| 4.1.1 Parsing | Not Applicable | Removed in WCAG 2.2 and treated as always met for 2.1. |
| 4.1.2 Name, Role, Value | Partially Supports | Axe passes. Custom widgets are not yet confirmed with a screen reader. |

## Table 2: WCAG 2.1 Level AA

| Criterion | Conformance | Remarks |
| --- | --- | --- |
| 1.2.4 Captions (Live) | Not Applicable | No live media. |
| 1.2.5 Audio Description (Prerecorded) | Not Applicable | As 1.2.1. |
| 1.3.4 Orientation | Not Evaluated | |
| 1.3.5 Identify Input Purpose | Partially Supports | Sign-in fields declare `autocomplete`. Others not reviewed. |
| 1.4.3 Contrast (Minimum) | Supports | Axe passes at 4.5:1 on the covered screens in the states scanned. |
| 1.4.4 Resize Text | Not Evaluated | |
| 1.4.5 Images of Text | Not Evaluated | |
| 1.4.10 Reflow | Not Evaluated | |
| 1.4.11 Non-text Contrast | Not Evaluated | Axe does not test this. The feedback trigger was darkened, but borders and icons were not measured. |
| 1.4.12 Text Spacing | Not Evaluated | |
| 1.4.13 Content on Hover or Focus | Not Evaluated | Tooltips appear on hover and focus; dismissal not tested. |
| 2.4.5 Multiple Ways | Not Evaluated | |
| 2.4.6 Headings and Labels | Partially Supports | Labels fixed. Headings not reviewed by hand. |
| 2.4.7 Focus Visible | Supports | 2px indigo ring across covered screens, asserted on the main flows. |
| 3.1.2 Language of Parts | Not Evaluated | |
| 3.2.3 Consistent Navigation | Not Evaluated | |
| 3.2.4 Consistent Identification | Not Evaluated | |
| 3.3.3 Error Suggestion | Not Evaluated | |
| 3.3.4 Error Prevention (Legal, Financial, Data) | Not Evaluated | |
| 4.1.3 Status Messages | Partially Supports | Agent status, completion, connection and banners go to a polite live region. Not confirmed with a screen reader. |

## Table 3: WCAG 2.1 Level AAA

Not evaluated. The product does not claim AAA.

## What this supports for the HECVAT today

| Question | Honest answer today |
| --- | --- |
| ITAC-06, conformance report available | Draft exists; not final |
| ITAC-08, substantially conforms to WCAG 2.1 AA | Not yet supportable: too many criteria are Not Evaluated |
| ITAC-17, all functions by keyboard | Yes with listed exceptions, not an unqualified Yes |

The source ticket also lists ITAC-11 as No, without saying what it asks. Check it separately.

## Before this can be submitted

1. Screen-reader pass (NVDA or JAWS, plus VoiceOver) on the covered screens.
2. Manual review of every Not Evaluated row.
3. Fix the out-of-baseline Rails screens or keep them out of scope explicitly.
4. Vendor contact and legal disclaimer, reviewed by whoever signs it.

---
"VPAT" is a registered trademark of the Information Technology Industry Council (ITI).
