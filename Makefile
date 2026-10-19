.PHONY: help init test run execute review triage logs smoke

ORCHESTRATOR_ENV_FILE ?= .env
ORCHESTRATOR_CONFIG_FILE ?= config/config.yaml
ORCHESTRATOR_DATA_DIR ?= ./data
export ORCHESTRATOR_ENV_FILE
export ORCHESTRATOR_CONFIG_FILE
export ORCHESTRATOR_DATA_DIR

help:
	@grep -E '^[a-zA-Z_-]+:' Makefile | sed 's/:.*//' | sort

init:
	uv run ai-orchestrator init --config-dir config --env-file .env --data-dir data

test:
	uv run pytest

run:
	uv run ai-orchestrator run $(REF)

execute:
	uv run ai-orchestrator execute

review:
	uv run ai-orchestrator review

triage:
	uv run ai-orchestrator triage

logs:
	uv run ai-orchestrator logs $(TASK) $(if $(NODE),--node $(NODE),)

smoke:
	bash /tmp/opencode/smoke.sh
