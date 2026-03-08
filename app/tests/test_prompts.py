"""
Tests for the Prompt API endpoints.

These tests verify prompt CRUD operations, especially
handling of large content that exceeds varchar(10000) limits.
"""
import pytest
from unittest.mock import MagicMock, patch
from main import app
from app.dependencies import auth, get_db_session
from app.models import Prompt


class TestPromptLargeContent:
    """Test that prompts can handle content larger than 10,000 characters.

    This validates the TEXT column type migration (revision 005) which
    changed prompt.content from varchar(10000) to TEXT.
    """

    @pytest.fixture
    def mock_session(self):
        """Create a mock database session."""
        session = MagicMock()
        return session

    @pytest.fixture
    def large_content(self):
        """Generate content larger than 10,000 characters."""
        # Create ~15,000 character content to exceed old varchar(10000) limit
        base_text = """# Data Import Protocol

You are importing data from `{{SOURCE_FILE_PATH}}` into the `{{TARGET_MODEL}}` model.

## Instructions

1. Parse the source file carefully
2. Map fields to the target model
3. Validate all data before import
4. Handle errors gracefully

"""
        # Repeat to exceed 10,000 chars
        return base_text * 50  # ~15,000 characters

    @pytest.mark.asyncio
    async def test_create_prompt_with_large_content(self, mock_session, large_content):
        """Test creating a prompt with content > 10,000 characters."""
        assert len(large_content) > 10000, "Test content should exceed 10,000 characters"

        created_prompt = Prompt(
            id=1,
            name="Large Test Prompt",
            content=large_content,
            group="Testing",
            description="A prompt with large content"
        )

        # Mock the session to return our created prompt
        mock_session.add = MagicMock()
        mock_session.commit = MagicMock()
        mock_session.refresh = MagicMock(side_effect=lambda p: setattr(p, 'id', 1))

        app.dependency_overrides[auth] = lambda: "testuser"
        app.dependency_overrides[get_db_session] = lambda: mock_session

        try:
            from fastapi.testclient import TestClient
            with TestClient(app) as client:
                response = client.post("/api/prompts", json={
                    "name": "Large Test Prompt",
                    "content": large_content,
                    "group": "Testing",
                    "description": "A prompt with large content"
                })

                # Should succeed (200 or 201), not fail with truncation error
                assert response.status_code in [200, 201], f"Expected success, got {response.status_code}: {response.text}"

                # Verify session.add was called with full content
                add_call = mock_session.add.call_args
                if add_call:
                    added_prompt = add_call[0][0]
                    assert len(added_prompt.content) == len(large_content), \
                        "Content should not be truncated"
        finally:
            app.dependency_overrides = {}

    @pytest.mark.asyncio
    async def test_update_prompt_with_large_content(self, mock_session, large_content):
        """Test updating a prompt with content > 10,000 characters."""
        assert len(large_content) > 10000, "Test content should exceed 10,000 characters"

        existing_prompt = Prompt(
            id=3,
            name="Existing Prompt",
            content="Original short content",
            group="Testing"
        )

        # Mock get to return existing prompt
        mock_session.get = MagicMock(return_value=existing_prompt)
        mock_session.add = MagicMock()
        mock_session.commit = MagicMock()
        mock_session.refresh = MagicMock()

        app.dependency_overrides[auth] = lambda: "testuser"
        app.dependency_overrides[get_db_session] = lambda: mock_session

        try:
            from fastapi.testclient import TestClient
            with TestClient(app) as client:
                response = client.patch("/api/prompts/3", json={
                    "content": large_content
                })

                # Should succeed, not fail with StringDataRightTruncation
                assert response.status_code == 200, f"Expected 200, got {response.status_code}: {response.text}"

                # Verify the prompt content was updated to full length
                assert existing_prompt.content == large_content, \
                    "Content should be updated to full large content"
                assert len(existing_prompt.content) > 10000, \
                    "Content length should exceed old varchar limit"
        finally:
            app.dependency_overrides = {}

    def test_prompt_model_accepts_large_content(self, large_content):
        """Test that the Prompt model can hold content > 10,000 characters."""
        assert len(large_content) > 10000

        prompt = Prompt(
            name="Test",
            content=large_content,
            group="Test"
        )

        assert prompt.content == large_content
        assert len(prompt.content) == len(large_content)

    def test_content_length_boundaries(self):
        """Test various content lengths around the old 10,000 char limit."""
        test_cases = [
            ("exactly_10000", "x" * 10000),
            ("just_over_10000", "x" * 10001),
            ("15000_chars", "x" * 15000),
            ("20000_chars", "x" * 20000),
            ("50000_chars", "x" * 50000),
        ]

        for name, content in test_cases:
            prompt = Prompt(
                name=f"Test {name}",
                content=content,
                group="Test"
            )
            assert len(prompt.content) == len(content), \
                f"Failed for {name}: expected {len(content)}, got {len(prompt.content)}"


class TestPromptAPI:
    """General tests for prompt API endpoints."""

    @pytest.fixture
    def mock_session(self):
        """Create a mock database session."""
        session = MagicMock()
        return session

    @pytest.mark.asyncio
    async def test_get_prompts(self, mock_session):
        """Test listing prompts."""
        mock_prompts = [
            Prompt(id=1, name="Prompt 1", content="Content 1", group="General"),
            Prompt(id=2, name="Prompt 2", content="Content 2", group="Testing"),
        ]

        mock_result = MagicMock()
        mock_result.all.return_value = mock_prompts
        mock_session.exec = MagicMock(return_value=mock_result)

        app.dependency_overrides[auth] = lambda: "testuser"
        app.dependency_overrides[get_db_session] = lambda: mock_session

        try:
            from fastapi.testclient import TestClient
            with TestClient(app) as client:
                response = client.get("/api/prompts")
                assert response.status_code == 200
                data = response.json()
                assert len(data) == 2
        finally:
            app.dependency_overrides = {}

    @pytest.mark.asyncio
    async def test_get_prompt_groups(self, mock_session):
        """Test getting prompt groups."""
        mock_result = MagicMock()
        mock_result.all.return_value = ["General", "Testing", "Data Import"]
        mock_session.exec = MagicMock(return_value=mock_result)

        app.dependency_overrides[auth] = lambda: "testuser"
        app.dependency_overrides[get_db_session] = lambda: mock_session

        try:
            from fastapi.testclient import TestClient
            with TestClient(app) as client:
                response = client.get("/api/prompts/groups")
                assert response.status_code == 200
                data = response.json()
                assert "groups" in data
        finally:
            app.dependency_overrides = {}
