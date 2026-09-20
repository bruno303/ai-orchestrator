IMAGE := bruno303/ai-orchestrator-agent:latest
INSTALL_OPENCODE ?= 1
INSTALL_CODEX ?= 0
INSTALL_CLAUDE ?= 0

.PHONY: help test run execute review triage logs smoke build-image publish-image

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

triage:
	uv run orchestrator triage

logs:
	uv run orchestrator logs $(TASK) $(if $(NODE),--node $(NODE),)

smoke:
	bash /tmp/opencode/smoke.sh

build-image:
	docker build -f Dockerfile.agent -t $(IMAGE) \
		--build-arg INSTALL_OPENCODE=$(INSTALL_OPENCODE) \
		--build-arg INSTALL_CODEX=$(INSTALL_CODEX) \
		--build-arg INSTALL_CLAUDE=$(INSTALL_CLAUDE) .

publish-image: build-image
	docker push $(IMAGE)
