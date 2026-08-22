.PHONY: help test run execute review logs smoke

help:
	@grep -E '^[a-zA-Z_-]+:' Makefile | sed 's/:.*//' | sort

test:
	uv run pytest

run:
	uv run orchestrator run $(REF)

execute:
	uv run orchestrator execute

review:
	uv run orchestrator review

logs:
	uv run orchestrator logs $(TASK) $(if $(NODE),--node $(NODE),)

smoke:
	bash /tmp/opencode/smoke.sh
