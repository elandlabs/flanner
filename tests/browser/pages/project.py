"""Adopting a repository, and what a project's page offers."""

from __future__ import annotations

from playwright.sync_api import Locator, Page


class NewProjectPage:
    """The adopt-a-project form.

    Only the text path. The Browse buttons post to `/api/folder-dialog`,
    which opens a native OS dialog on the machine running the server; a
    headless runner has nobody to close it.
    """

    def __init__(self, page: Page) -> None:
        self.page = page

    def open(self) -> None:
        self.page.goto("/projects/new")

    def fill(self, *, name: str, root: str, description: str = "") -> None:
        self.page.get_by_role("textbox", name="Project name").fill(name)
        if description:
            self.page.get_by_role("textbox", name="Description").fill(description)
        # By role, not by label: each path field has a Browse button beside
        # it whose accessible name contains the same words.
        self.page.get_by_role("textbox", name="Project root").fill(root)

    @property
    def submit(self) -> Locator:
        return self.page.get_by_role("button", name="Create project")

    @property
    def error(self) -> Locator:
        return self.page.get_by_role("alert")


class ProjectPage:
    def __init__(self, page: Page) -> None:
        self.page = page

    def open(self, project_id: str) -> None:
        self.page.goto(f"/projects/{project_id}")

    @property
    def new_plan(self) -> Locator:
        return self.page.get_by_role("link", name="New plan").first

    def plan(self, name: str) -> Locator:
        return self.page.get_by_role("link", name=f"{name}.md")


class NewPlanPage:
    def __init__(self, page: Page) -> None:
        self.page = page

    def fill(self, *, name: str, content: str, description: str = "") -> None:
        self.page.get_by_role("textbox", name="Plan name").fill(name)
        if description:
            self.page.get_by_role("textbox", name="Description").fill(description)
        self.page.get_by_role("textbox", name="Content").fill(content)

    @property
    def submit(self) -> Locator:
        return self.page.get_by_role("button", name="Create plan")
