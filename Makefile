.PHONY: help test run poll list status resume reset logs watch smoke

help:
	@grep -E '^[a-zA-Z_-]+:' Makefile | sed 's/:.*//' | sort

test:
	uv run pytest

run:
	uv run orchestrator run $(REF)

poll:
	uv run orchestrator poll

list:
	uv run orchestrator list

status:
	uv run orchestrator status $(TASK)

resume:
	uv run orchestrator resume $(TASK)

reset:
	uv run orchestrator reset $(TASK)

logs:
	uv run orchestrator logs $(TASK) $(if $(NODE),--node $(NODE),) $(if $(FOLLOW),--follow,)

watch:
	uv run orchestrator watch

smoke:
	bash /tmp/opencode/smoke.sh