"""Application service for creating and persisting Stage 1 mock runs."""

from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy.orm import Session

from agents.macro.agent import USMacroCycleAgent
from agents.macro.schemas import MacroCycleResult
from app.database.models import AgentReport, AgentRun
from app.database.repositories.runs import add_run_with_report
from app.settings import Settings


async def create_mock_run(session: Session, settings: Settings) -> MacroCycleResult:
    """Execute the mock lifecycle and atomically stage a run and report."""
    started_at = datetime.now(UTC)
    agent = USMacroCycleAgent(settings)
    await agent.validate_dependencies()
    inputs = await agent.collect_inputs()
    analysis = await agent.run_analysis(inputs)
    result = await agent.build_result(analysis)
    markdown = await agent.generate_report(result)
    await agent.persist_result(result, markdown)
    finished_at = datetime.now(UTC)

    run = AgentRun(
        run_id=result.run_id,
        agent_name=result.agent,
        agent_version=result.version,
        status="completed",
        started_at=started_at,
        finished_at=finished_at,
        duration_seconds=Decimal(str((finished_at - started_at).total_seconds())),
        data_quality_status=result.data_quality_status,
        llm_mode=settings.llm_mode,
        input_tokens=0,
        output_tokens=0,
        estimated_cost=Decimal("0"),
    )
    report = AgentReport(
        run_id=result.run_id,
        report_type="macro_cycle",
        result_json=result.model_dump(mode="json"),
        markdown_content=markdown,
        llm_provider=result.llm_provider,
        llm_model=result.llm_model,
        prompt_version="0.1.0",
    )
    add_run_with_report(session, run, report)
    return result
