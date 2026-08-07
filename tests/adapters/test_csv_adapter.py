import pytest

from orchestration.adapters.task_source.csv import CsvAdapter


@pytest.mark.asyncio
async def test_csv_list_tasks(tmp_path):
    csv = tmp_path / "tasks.csv"
    csv.write_text("external_id,title,description\nT1,Test 1,Desc 1\nT2,Test 2,Desc 2\n")
    adapter = CsvAdapter(path=str(csv))
    tasks = await adapter.list_tasks(project_id=None, status=None)
    assert len(tasks) == 2
    assert tasks[0].external_id == "T1"


@pytest.mark.asyncio
async def test_csv_create_task(tmp_path):
    csv = tmp_path / "tasks.csv"
    csv.write_text("external_id,title,description\n")
    adapter = CsvAdapter(path=str(csv))
    task = await adapter.create_task(
        project_id=None,
        payload=type("P", (), {"title": "New", "description": "New desc"})(),
    )
    assert task.external_id == "T1"


@pytest.mark.asyncio
async def test_csv_add_comment(tmp_path):
    csv = tmp_path / "tasks.csv"
    csv.write_text("external_id,title,description\nT1,Test,\n")
    adapter = CsvAdapter(path=str(csv))
    c = await adapter.add_comment("T1", "looks good")
    assert c.body == "looks good"
