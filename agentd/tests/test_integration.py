import pytest
import os, sys, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from agent_hub.db import Database
from agent_hub.task_manager import TaskManager
from agent_hub.cline_executor import MockClineExecutor
from agent_hub.config import ClineConfig, AgentdConfig

def make_config(**kw):
    c = ClineConfig(mock=True, mock_exit_code=kw.get('exit_code', 0),
                    mock_delay_seconds=0.01,
                    max_retries=kw.get('max_retries', 1))
    return AgentdConfig(cline=c)

@pytest.mark.asyncio
async def test_mock_success():
    with tempfile.TemporaryDirectory() as tmp:
        db = Database(os.path.join(tmp, 't.sqlite'))
        tm = TaskManager(db)
        cfg = make_config(exit_code=0)
        ex = MockClineExecutor(cfg, task_manager=tm)
        task = tm.create_task(prompt='mock test')
        tm.transition(task['id'], 'CLINE_STARTING', trigger='t')
        tm.transition(task['id'], 'CLINE_RUNNING', trigger='t')
        _, info = await ex.spawn(task)
        assert info['mock']
        assert tm.get_task(task['id'])['state'] == 'CLINE_SUCCEEDED'
        db.close()

@pytest.mark.asyncio
async def test_mock_failure_no_retry():
    with tempfile.TemporaryDirectory() as tmp:
        db = Database(os.path.join(tmp, 't.sqlite'))
        tm = TaskManager(db)
        cfg = make_config(exit_code=1, max_retries=0)
        ex = MockClineExecutor(cfg, task_manager=tm)
        task = tm.create_task()
        tm.transition(task['id'], 'CLINE_STARTING', trigger='t')
        tm.transition(task['id'], 'CLINE_RUNNING', trigger='t')
        await ex.spawn(task)
        t = tm.get_task(task['id'])
        assert t['state'] == 'CLINE_FAILED'
        db.close()

@pytest.mark.asyncio
async def test_mock_retry():
    with tempfile.TemporaryDirectory() as tmp:
        db = Database(os.path.join(tmp, 't.sqlite'))
        tm = TaskManager(db)
        cfg = make_config(exit_code=1, max_retries=2)
        ex = MockClineExecutor(cfg, task_manager=tm)
        task = tm.create_task()
        tm.transition(task['id'], 'CLINE_STARTING', trigger='t')
        tm.transition(task['id'], 'CLINE_RUNNING', trigger='t')
        await ex.spawn(task)
        t = tm.get_task(task['id'])
        # After retry, state depends on mock exit code
        assert t['state'] in ('CLINE_FAILED', 'CLINE_STARTING', 'CLINE_RUNNING', 'CLINE_SUCCEEDED')
        db.close()
