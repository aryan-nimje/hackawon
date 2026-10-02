"""Integration test for full mock-mode pipeline."""

import pytest

from supervisor import create_run, run_pipeline


@pytest.mark.asyncio
async def test_full_mock_pipeline():
    run = create_run()
    completed = await run_pipeline(run)
    assert completed.status.value == "completed"
    assert len(completed.incidents) > 0
    assert len(completed.verifications) > 0
    assert len(completed.zones) > 0
    assert len(completed.rescue_queue) > 0
    assert completed.plan is not None
    assert len(completed.plan.items) > 0
    assert len(completed.alerts) == 3
    assert completed.plan.is_final is False
