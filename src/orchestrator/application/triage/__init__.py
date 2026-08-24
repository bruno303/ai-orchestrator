"""Provider-neutral issue triage workflow."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Callable

from orchestrator.application.ports import (
    ContextPresenter, NoopContextPresenter, TriageDestination, TriageExecutor,
    TriageInputSource, TriageRequest,
)
from orchestrator.domain import TriageOutcome, TriageTarget
from orchestrator.application.triage.service import TRIAGE_PROMPT


@dataclass
class TriageApplication:
    input_source: TriageInputSource
    executor: TriageExecutor
    destination: TriageDestination
    context_presenter: ContextPresenter
    task_log_path: Callable[[str, str], Path]
    write_task_log: Callable[[str, str, str], None]

    def __init__(
        self,
        input_source: TriageInputSource,
        executor: TriageExecutor,
        destination: TriageDestination,
        *,
        context_presenter: ContextPresenter | None = None,
        task_log_path: Callable[[str, str], Path] | None = None,
        write_task_log: Callable[[str, str, str], None] | None = None,
    ) -> None:
        self.input_source = input_source
        self.executor = executor
        self.destination = destination
        self.context_presenter = context_presenter or getattr(
            input_source, "context_presenter", NoopContextPresenter()
        )
        self.task_log_path = task_log_path or (lambda task_id, node: Path(f"{task_id}-{node}.log"))
        self.write_task_log = write_task_log or (lambda _task_id, _node, _message: None)

    def poll_once(self) -> list[TriageTarget]:
        processed: list[TriageTarget] = []
        for target in self.input_source.poll():
            title = " ".join(target.title.split()) or "<untitled>"
            fields = dict(self.context_presenter.logging_fields(target.context))
            self.write_task_log(
                target.id, "triage",
                f"[triage] starting: repository={target.repository} id={target.id} "
                f"title={title!r} context={fields}",
            )
            print(f"[triage] starting: repository={target.repository} id={target.id} title={title!r}", flush=True)
            try:
                with TemporaryDirectory(prefix="orchestrator-triage-") as directory:
                    outcome = self.executor.execute(TriageRequest(
                        task_id=target.id,
                        repository=target.repository,
                        workspace=directory,
                        prompt=TRIAGE_PROMPT.format(
                            task_id=target.id,
                            repository=target.repository,
                            title=target.title,
                            description=target.description,
                        ),
                        context=target.context,
                        log_file=str(self.task_log_path(target.id, "triage")),
                    ))
                    if not isinstance(outcome, TriageOutcome):
                        raise TypeError("triage executor returned an invalid outcome")
                    if not outcome.success:
                        raise RuntimeError(outcome.summary or "triage execution failed")
                    self.destination.publish(target, outcome)
                processed.append(target)
                self.write_task_log(target.id, "triage", f"[triage] finished: repository={target.repository} id={target.id}")
                print(f"[triage] finished: repository={target.repository} id={target.id}", flush=True)
            except Exception as exc:
                print(f"[triage] {target.id}: {exc}", flush=True)
        return processed
