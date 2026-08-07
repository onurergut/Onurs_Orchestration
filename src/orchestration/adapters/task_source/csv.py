import csv as csvlib
from pathlib import Path
from uuid import UUID

from orchestration.adapters.task_source.base import (
    Comment,
    CreateTask,
    ExternalTask,
    TaskSourceAdapter,
)


class CsvAdapter(TaskSourceAdapter):
    name = "csv"

    def __init__(self, path: str):
        self.path = Path(path)

    def _read(self) -> list[dict]:
        with self.path.open() as f:
            return list(csvlib.DictReader(f))

    async def list_tasks(self, project_id: UUID, status: str | None) -> list[ExternalTask]:
        return [
            ExternalTask(
                external_id=row["external_id"],
                title=row["title"],
                description=row.get("description"),
            )
            for row in self._read()
        ]

    async def get_task(self, task_id: str) -> ExternalTask:
        for row in self._read():
            if row["external_id"] == task_id:
                return ExternalTask(
                    external_id=row["external_id"],
                    title=row["title"],
                    description=row.get("description"),
                )
        raise KeyError(task_id)

    async def create_task(self, project_id: UUID, payload: CreateTask) -> ExternalTask:
        # Idempotency handled by caller via external_receipts
        existing = self._read()
        new_id = f"T{len(existing) + 1}"
        with self.path.open("a") as f:
            writer = csvlib.DictWriter(f, fieldnames=["external_id", "title", "description"])
            writer.writerow({"external_id": new_id, "title": payload.title, "description": payload.description})
        return ExternalTask(external_id=new_id, title=payload.title, description=payload.description)

    async def update_status(self, task_id: str, status: str) -> None:
        # Read-modify-write
        rows = self._read()
        for row in rows:
            if row["external_id"] == task_id:
                row["status"] = status
        with self.path.open("w") as f:
            writer = csvlib.DictWriter(f, fieldnames=["external_id", "title", "description", "status"])
            writer.writeheader()
            writer.writerows(rows)

    async def add_comment(self, task_id: str, body: str) -> Comment:
        # Append to .comments.csv sidecar
        comments_path = self.path.with_suffix(".comments.csv")
        with comments_path.open("a") as f:
            writer = csvlib.DictWriter(f, fieldnames=["external_id", "body"])
            writer.writerow({"external_id": task_id, "body": body})
        return Comment(external_id=task_id, body=body)
