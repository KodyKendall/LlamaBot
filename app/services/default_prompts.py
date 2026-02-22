"""Default prompts that are seeded into the database on startup."""

DEFAULT_PROMPTS = [
    # Engineering prompts
    {
        "name": "Red Green Test",
        "group": "Engineering",
        "description": "Create failing tests that reproduce an issue (for Test Builder mode)",
        "content": """You are in RED phase of Red-Green-Refactor TDD.

Given the ticket below, your goal is to write one or more **failing tests** that perfectly reproduce the issue or define the expected behavior for the feature.

## Requirements:
1. **Tests MUST fail** - They should fail because the fix/feature doesn't exist yet
2. **Tests should be precise** - They should fail for the RIGHT reason (the missing functionality)
3. **Tests should be minimal** - Only test what's needed to verify the fix
4. **Tests should be clear** - Someone reading the test should understand what's being fixed

## Output:
- Write the failing test(s)
- Explain WHY each test fails (what's missing in the code)
- Confirm the test will pass once the fix is implemented

## Ticket:
[Paste ticket here]"""
    },
    {
        "name": "Task Decomposition",
        "group": "Engineering",
        "description": "Break down a large ticket into 2-3 smaller sequential tickets (for Ticket Mode)",
        "content": """This ticket is too large to implement in one pass. Break it down into 2-3 smaller, sequential tickets.

## Requirements:
1. **Number each ticket** with format: "Original Title (1/N)", "Original Title (2/N)", etc.
2. **Each ticket must be independently implementable** - Can be completed and tested on its own
3. **Order matters** - Earlier tickets should not depend on later ones
4. **Include a test plan** for each ticket - What tests verify this piece works?

## Output Format for each sub-ticket:

### [Original Title] (X/N)

**Summary:** [What this specific piece accomplishes]

**Acceptance Criteria:**
- [ ] Criterion 1
- [ ] Criterion 2

**Test Plan:**
- Test 1: [Description of test]
- Test 2: [Description of test]

**Dependencies:** [What must be done before this ticket, if any]

---

## Original Ticket to Decompose:
[Paste ticket here]"""
    },
]
