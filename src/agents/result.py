from __future__ import annotations

import abc
import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

from typing_extensions import TypeVar

from ._run_impl import QueueCompleteSentinel
from .agent import Agent
from .agent_output import AgentOutputSchema
from .exceptions import InputGuardrailTripwireTriggered, MaxTurnsExceeded
from .guardrail import InputGuardrailResult, OutputGuardrailResult
from .items import ItemHelpers, ModelResponse, RunItem, TResponseInputItem
from .logger import logger
from .stream_events import StreamEvent
from .tracing import Trace
from .util._pretty_print import pretty_print_result, pretty_print_run_result_streaming

if TYPE_CHECKING:
    from ._run_impl import QueueCompleteSentinel
    from .agent import Agent

T = TypeVar("T")


@dataclass
class RunResultBase(abc.ABC):
    input: str | list[TResponseInputItem]
    """The original input items i.e. the items before run() was called. This may be a mutated
    version of the input, if there are handoff input filters that mutate the input.
    """

    new_items: list[RunItem]
    """The new items generated during the agent run. These include things like new messages, tool
    calls and their outputs, etc.
    """

    raw_responses: list[ModelResponse]
    """The raw LLM responses generated by the model during the agent run."""

    final_output: Any
    """The output of the last agent."""

    input_guardrail_results: list[InputGuardrailResult]
    """Guardrail results for the input messages."""

    output_guardrail_results: list[OutputGuardrailResult]
    """Guardrail results for the final output of the agent."""

    @property
    @abc.abstractmethod
    def last_agent(self) -> Agent[Any]:
        """The last agent that was run."""

    def final_output_as(self, cls: type[T], raise_if_incorrect_type: bool = False) -> T:
        """A convenience method to cast the final output to a specific type. By default, the cast
        is only for the typechecker. If you set `raise_if_incorrect_type` to True, we'll raise a
        TypeError if the final output is not of the given type.

        Args:
            cls: The type to cast the final output to.
            raise_if_incorrect_type: If True, we'll raise a TypeError if the final output is not of
                the given type.

        Returns:
            The final output casted to the given type.
        """
        if raise_if_incorrect_type and not isinstance(self.final_output, cls):
            raise TypeError(f"Final output is not of type {cls.__name__}")

        return cast(T, self.final_output)

    def to_input_list(self) -> list[TResponseInputItem]:
        """Creates a new input list, merging the original input with all the new items generated."""
        original_items: list[TResponseInputItem] = ItemHelpers.input_to_new_input_list(self.input)
        new_items = [item.to_input_item() for item in self.new_items]

        return original_items + new_items


@dataclass
class RunResult(RunResultBase):
    _last_agent: Agent[Any]

    @property
    def last_agent(self) -> Agent[Any]:
        """The last agent that was run."""
        return self._last_agent

    def __str__(self) -> str:
        return pretty_print_result(self)


@dataclass
class RunResultStreaming(RunResultBase):
    """The result of an agent run in streaming mode. You can use the `stream_events` method to
    receive semantic events as they are generated.

    The streaming method will raise:
    - A MaxTurnsExceeded exception if the agent exceeds the max_turns limit.
    - A GuardrailTripwireTriggered exception if a guardrail is tripped.
    """

    current_agent: Agent[Any]
    """The current agent that is running."""

    current_turn: int
    """The current turn number."""

    max_turns: int
    """The maximum number of turns the agent can run for."""

    final_output: Any
    """The final output of the agent. This is None until the agent has finished running."""

    _current_agent_output_schema: AgentOutputSchema | None = field(repr=False)

    _trace: Trace | None = field(repr=False)

    is_complete: bool = False
    """Whether the agent has finished running."""

    # Queues that the background run_loop writes to
    _event_queue: asyncio.Queue[StreamEvent | QueueCompleteSentinel] = field(
        default_factory=asyncio.Queue, repr=False
    )
    _input_guardrail_queue: asyncio.Queue[InputGuardrailResult] = field(
        default_factory=asyncio.Queue, repr=False
    )

    # Store the asyncio tasks that we're waiting on
    _run_impl_task: asyncio.Task[Any] | None = field(default=None, repr=False)
    _input_guardrails_task: asyncio.Task[Any] | None = field(default=None, repr=False)
    _output_guardrails_task: asyncio.Task[Any] | None = field(default=None, repr=False)
    _stored_exception: Exception | None = field(default=None, repr=False)

    @property
    def last_agent(self) -> Agent[Any]:
        """The last agent that was run. Updates as the agent run progresses, so the true last agent
        is only available after the agent run is complete.
        """
        return self.current_agent

    async def stream_events(self) -> AsyncIterator[StreamEvent]:
        """Stream deltas for new items as they are generated. We're using the types from the
        OpenAI Responses API, so these are semantic events: each event has a `type` field that
        describes the type of the event, along with the data for that event.

        This will raise:
        - A MaxTurnsExceeded exception if the agent exceeds the max_turns limit.
        - A GuardrailTripwireTriggered exception if a guardrail is tripped.
        """
        while True:
            self._check_errors()
            if self._stored_exception:
                logger.debug("Breaking due to stored exception")
                self.is_complete = True
                break

            if self.is_complete and self._event_queue.empty():
                break

            try:
                item = await self._event_queue.get()
            except asyncio.CancelledError:
                break

            if isinstance(item, QueueCompleteSentinel):
                self._event_queue.task_done()
                # Check for errors, in case the queue was completed due to an exception
                self._check_errors()
                break

            yield item
            self._event_queue.task_done()

        if self._trace:
            self._trace.finish(reset_current=True)

        self._cleanup_tasks()

        if self._stored_exception:
            raise self._stored_exception

    def _check_errors(self):
        if self.current_turn > self.max_turns:
            self._stored_exception = MaxTurnsExceeded(f"Max turns ({self.max_turns}) exceeded")

        # Fetch all the completed guardrail results from the queue and raise if needed
        while not self._input_guardrail_queue.empty():
            guardrail_result = self._input_guardrail_queue.get_nowait()
            if guardrail_result.output.tripwire_triggered:
                self._stored_exception = InputGuardrailTripwireTriggered(guardrail_result)

        # Check the tasks for any exceptions
        if self._run_impl_task and self._run_impl_task.done():
            exc = self._run_impl_task.exception()
            if exc and isinstance(exc, Exception):
                self._stored_exception = exc

        if self._input_guardrails_task and self._input_guardrails_task.done():
            exc = self._input_guardrails_task.exception()
            if exc and isinstance(exc, Exception):
                self._stored_exception = exc

        if self._output_guardrails_task and self._output_guardrails_task.done():
            exc = self._output_guardrails_task.exception()
            if exc and isinstance(exc, Exception):
                self._stored_exception = exc

    def _cleanup_tasks(self):
        if self._run_impl_task and not self._run_impl_task.done():
            self._run_impl_task.cancel()

        if self._input_guardrails_task and not self._input_guardrails_task.done():
            self._input_guardrails_task.cancel()

        if self._output_guardrails_task and not self._output_guardrails_task.done():
            self._output_guardrails_task.cancel()

    def __str__(self) -> str:
        return pretty_print_run_result_streaming(self)
