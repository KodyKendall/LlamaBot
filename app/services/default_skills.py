"""Default skills to seed into the database on first run."""

DEFAULT_SKILLS = [
    {
        "name": "Code Review Focus",
        "group": "Engineering",
        "description": "Adds code review best practices and attention to detail",
        "content": """When reviewing or writing code, pay special attention to:

1. **Security**: Look for potential vulnerabilities (SQL injection, XSS, authentication bypasses, secrets in code)
2. **Error Handling**: Ensure proper error handling and edge cases are covered
3. **Performance**: Watch for N+1 queries, unnecessary loops, memory leaks
4. **Code Style**: Follow existing patterns and naming conventions in the codebase
5. **Testing**: Consider what tests should accompany any code changes

If you find issues, explain them clearly and suggest fixes."""
    },
    {
        "name": "Test-First Mindset",
        "group": "Engineering",
        "description": "Encourages writing tests before or alongside implementation",
        "content": """Approach this task with a test-first mindset:

1. **Think about test cases first**: What inputs should work? What edge cases exist? What should fail?
2. **Write failing tests**: Create tests that define the expected behavior before implementing
3. **Keep tests focused**: Each test should verify one specific behavior
4. **Test edge cases**: Empty inputs, null values, boundary conditions, error states
5. **Make tests readable**: Test names should describe the scenario being tested

When implementing, ensure all tests pass before considering the task complete."""
    },
    {
        "name": "Explain Your Reasoning",
        "group": "Communication",
        "description": "Asks for detailed explanations of decisions and trade-offs",
        "content": """As you work on this task, please explain your reasoning:

1. **Why this approach?**: Explain why you chose this solution over alternatives
2. **Trade-offs**: What are the pros and cons of your approach?
3. **Assumptions**: What assumptions are you making? Are they valid?
4. **Risks**: What could go wrong? How can we mitigate those risks?
5. **Future considerations**: How might this need to change as the system evolves?

This helps me learn and ensures we're aligned on the approach."""
    },
]
