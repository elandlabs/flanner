"""Adopting a repository, writing a plan, revising it, and reading it back.

These are the journeys where a silent break costs a plan. Everything here
drives the same controls a person does; nothing posts to a route directly.
"""

from __future__ import annotations

import re
import subprocess
import uuid
from pathlib import Path
from typing import Any

import pytest
from playwright.sync_api import Page, expect

from tests.browser.pages.plan import PlanEditPage, PlanHistoryPage, PlanPage
from tests.browser.pages.project import NewPlanPage, NewProjectPage, ProjectPage
from tests.browser.pages.shell import Shell


@pytest.fixture
def empty_repo(tmp_path: Path) -> Path:
    """A git repository with one commit, for the project to be pointed at."""
    root = tmp_path / "ledger-service"
    root.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=True)  # noqa: S603, S607
    (root / "README.md").write_text("# ledger-service\n", encoding="utf-8")
    env = {
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t.invalid",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t.invalid",
    }
    subprocess.run(["git", "-C", str(root), "add", "."], check=True, env={**env})  # noqa: S603, S607
    subprocess.run(  # noqa: S603
        ["git", "-C", str(root), "commit", "-q", "-m", "first"],  # noqa: S607
        check=True,
        env={**env},
    )
    return root


def test_a_repository_can_be_adopted_through_the_text_input(page: Page, empty_repo: Path) -> None:
    """The Browse buttons are never touched.

    `POST /api/folder-dialog` opens a native OS dialog on the machine running
    the server, which on a headless runner is a dialog nobody can close.
    """
    form = NewProjectPage(page)
    form.open()
    form.fill(
        name=f"ledger-{uuid.uuid4().hex[:8]}",
        root=str(empty_repo),
        description="Double-entry, and the reports over it.",
    )
    form.submit.click()

    expect(page).to_have_url(re.compile(r"/projects/[0-9a-f-]{36}$"))
    expect(page.get_by_role("heading", name="ledger-", exact=False)).to_be_visible()


def test_a_folder_that_is_not_a_repository_is_refused_with_what_was_typed(
    page: Page, tmp_path: Path
) -> None:
    """A validation error must not make somebody retype the form."""
    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    form = NewProjectPage(page)
    form.open()
    form.fill(name="not-a-repo", root=str(plain))
    form.submit.click()

    expect(form.error).to_contain_text("not a valid git repository")
    expect(page.get_by_label("Project name")).to_have_value("not-a-repo")


def test_a_plan_is_created_revised_and_shows_version_two_in_its_history(
    page: Page, catalog: dict[str, Any]
) -> None:
    """Create, edit in CodeMirror, save, and find v2 in the history.

    Its own plan rather than a seeded one: this writes, and a journey that
    writes into the shared catalog decides what every later test sees.
    """
    name = f"cache-strategy-{uuid.uuid4().hex[:8]}"
    project = ProjectPage(page)
    project.open(catalog["projects"]["payments-service"]["id"])
    project.new_plan.click()

    NewPlanPage(page).fill(
        name=name,
        content="# Cache strategy\n\nRead-through, with a short ttl.\n",
        description="What is cached and for how long.",
    )
    NewPlanPage(page).submit.click()

    plan = PlanPage(page)
    expect(plan.body).to_contain_text("Read-through, with a short ttl.")
    plan_id = page.url.rstrip("/").split("/")[-1].split("?")[0]

    plan.edit.click()
    editor = PlanEditPage(page)
    expect(editor.editor).to_be_visible()
    editor.append("\n\n## Invalidation\n\nOn write, by key.\n")
    editor.notes.fill("say how invalidation works")
    editor.save.click()

    expect(plan.body).to_contain_text("On write, by key.")

    history = PlanHistoryPage(page)
    history.open(plan_id)
    expect(history.version(2)).to_be_visible()
    expect(page.get_by_text("say how invalidation works")).to_be_visible()


def test_reading_a_plan_offers_every_version_and_a_download_of_the_one_shown(
    page: Page, catalog: dict[str, Any]
) -> None:
    """The seeded five-version plan, read rather than written."""
    plan_id = catalog["plans"]["jwt-key-rotation"]
    plan = PlanPage(page)
    plan.open(plan_id)

    expect(plan.body).to_contain_text("Re-publish the previous public key.")
    expect(plan.version_selector).to_have_value("5")
    expect(plan.version_selector.get_by_role("option")).to_have_count(5)
    expect(plan.download).to_have_attribute("href", f"/plans/{plan_id}/download?version=5")

    plan.version_selector.select_option("2")
    expect(plan.body).to_contain_text("Rotation lives in")
    expect(plan.body).not_to_contain_text("Re-publish the previous public key.")
    expect(plan.download).to_have_attribute("href", f"/plans/{plan_id}/download?version=2")


def test_a_retired_plan_still_opens_and_says_it_is_retired(
    page: Page, catalog: dict[str, Any]
) -> None:
    """A saved link explains itself rather than 404ing. Nothing was erased."""
    PlanPage(page).open(catalog["plans"]["idempotency-keys"])
    expect(page.get_by_text("This plan is retired.")).to_be_visible()
    expect(page.get_by_text("the gateway deduplicates now")).to_be_visible()


def test_an_id_that_leads_nowhere_renders_the_error_page(page: Page, shell: Shell) -> None:
    """Not a stack trace, and not a blank 404."""
    page.goto(f"/plans/{uuid.uuid4()}")
    expect(shell.heading).to_have_text("Page not found")
    expect(page.get_by_text("Nothing lives at this address")).to_be_visible()
    expect(page.get_by_role("link", name="Back to Dashboard")).to_be_visible()
