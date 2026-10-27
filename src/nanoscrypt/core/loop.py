import asyncio
from typing import Any, AsyncIterator

from nanoscrypt.core.events import (
    AgentStartEvent,
    AgentEndEvent,
    TurnStartEvent,
    TurnEndEvent,
    ThinkingDeltaEvent,
    MessageStartEvent,
    MessageDeltaEvent,
    MessageEndEvent,
    ToolExecutionStartEvent,
    ToolExecutionEndEvent
)


def _with_recent_conversation(history: list[dict[str, str]], prompt: str) -> str:
    """Provide bounded prior turns to the orchestrator as conversational context."""
    if not history:
        return prompt

    recent: list[dict[str, str]] = []
    remaining_chars = 24_000
    for message in reversed(history[-20:]):
        if remaining_chars <= 0:
            break
        content = message.get("content", "")
        if len(content) > remaining_chars:
            content = "[Earlier content truncated]\n" + content[-remaining_chars:]
        recent.append({"role": message.get("role", "user"), "content": content})
        remaining_chars -= len(content)
    transcript = "\n".join(
        f"{message['role'].capitalize()}: {message['content']}"
        for message in reversed(recent)
    )
    return (
        "Conversation so far (treat prior messages as context; the newest user request "
        "is the task to act on):\n"
        f"{transcript}\n\nCurrent user request:\n{prompt}"
    )


async def run_agent_loop(
    orchestrator: Any,
    user_prompt: str,
    session: Any,
    agent: Any = None,
    signal: Any = None,
    harness: Any = None,
) -> AsyncIterator[Any]:
    """Run a bounded conversation loop and yield progress events for each turn.

    Each turn is a complete orchestrator task. Queued steering and follow-ups are
    then executed as subsequent tasks with recent session conversation included.
    """
    yield AgentStartEvent()
    next_prompt: str | None = user_prompt
    max_turns = getattr(harness, "max_turns", 1)

    try:
        for turn in range(1, max_turns + 1):
            if signal and signal.is_cancelled():
                break
            if next_prompt is None:
                break

            yield TurnStartEvent(turn=turn)
            yield ThinkingDeltaEvent(delta="Working on the current request...")
            streamed_message_started = False

            async def stream_response_delta(delta: str) -> None:
                nonlocal streamed_message_started
                if harness is None:
                    return
                if not streamed_message_started:
                    await harness.emit_event(
                        MessageStartEvent(message_role="assistant")
                    )
                    streamed_message_started = True
                await harness.emit_event(MessageDeltaEvent(delta=delta))

            async def report_progress(message: str) -> None:
                if harness is not None:
                    from nanoscrypt.core.events import ProgressEvent

                    await harness.emit_event(ProgressEvent(message=message))

            if harness is not None:
                async def report_sandbox_output(event) -> None:
                    await harness.emit_event(event)

                async def report_sandbox_operation(event) -> None:
                    await harness.emit_event(event)

                report_progress.sandbox_output = report_sandbox_output
                report_progress.sandbox_operation = report_sandbox_operation

            session.active_prompt = next_prompt
            session.conversation_history.append(
                {"role": "user", "content": next_prompt}
            )
            prior_history = session.conversation_history[:-1]
            contextual_prompt = _with_recent_conversation(prior_history, next_prompt)
            execution = asyncio.create_task(
                orchestrator.execute_task(
                    user_prompt=contextual_prompt,
                    session=session,
                    agent=agent,
                    cancellation_event=(
                        signal.thread_event
                        if signal is not None and hasattr(signal, "thread_event")
                        else None
                    ),
                    stream_callback=(
                        stream_response_delta if harness is not None else None
                    ),
                    progress_callback=(
                        report_progress if harness is not None else None
                    ),
                )
            )
            if signal is None:
                result = await execution
            else:
                cancellation = asyncio.create_task(signal.wait_cancelled())
                done, _ = await asyncio.wait(
                    {execution, cancellation},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if cancellation in done and signal.is_cancelled():
                    execution.cancel()
                    try:
                        await execution
                    except asyncio.CancelledError:
                        pass
                    cancellation.cancel()
                    if harness is not None:
                        harness.last_result = {
                            "status": "cancelled",
                            "error": "Execution cancelled.",
                        }
                        harness.persist_session()
                    break
                cancellation.cancel()
                try:
                    await cancellation
                except asyncio.CancelledError:
                    pass
                result = await execution
            if harness is not None:
                harness.last_result = result

            reasoning = result.get("reasoning")
            if reasoning:
                yield ThinkingDeltaEvent(delta=f"\n{reasoning}")

            if result.get("action_taken") == "execute_tool":
                tool_name = result.get("tool_name", "unknown")
                yield ToolExecutionStartEvent(
                    tool_name=tool_name,
                    arguments={"query": next_prompt},
                )
                yield ToolExecutionEndEvent(
                    tool_name=tool_name,
                    success=result.get("status") == "completed",
                    output=result.get("output"),
                    error=result.get("error"),
                )

            output_content = str(
                result.get("output") or result.get("response") or ""
            )
            session.conversation_history.append(
                {"role": "assistant", "content": output_content}
            )
            # Bound in-memory session context while retaining recent turns.
            session.conversation_history = session.conversation_history[-40:]
            if harness is not None:
                harness.persist_session()

            if not result.get("streamed"):
                yield MessageStartEvent(message_role="assistant")
                for index in range(0, len(output_content), 120):
                    if signal and signal.is_cancelled():
                        break
                    yield MessageDeltaEvent(delta=output_content[index:index + 120])
            yield MessageEndEvent(
                message={"role": "assistant", "content": output_content}
            )
            yield TurnEndEvent(turn=turn)

            if result.get("status") in {"denied", "clarification_needed"}:
                break
            next_prompt = (
                harness.take_queued_input() if harness is not None else None
            )
    except Exception:
        yield AgentEndEvent()
        raise
    yield AgentEndEvent()
