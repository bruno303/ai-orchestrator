IMAGE_PREFIX ?= bruno303/ai-orchestrator-agent
OPENCODE_IMAGE := $(IMAGE_PREFIX)-opencode:latest
CODEX_IMAGE := $(IMAGE_PREFIX)-codex:latest
CLAUDE_IMAGE := $(IMAGE_PREFIX)-claude:latest

.PHONY: help test run execute review triage logs smoke build-image build-images build-opencode-image build-codex-image build-claude-image publish-image publish-images

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

build-opencode-image:
	docker build -f Dockerfile.agent --target opencode -t $(OPENCODE_IMAGE) .

build-codex-image:
	docker build -f Dockerfile.agent --target codex -t $(CODEX_IMAGE) .

build-claude-image:
	docker build -f Dockerfile.agent --target claude -t $(CLAUDE_IMAGE) .

build-images: build-opencode-image build-codex-image build-claude-image

# Backward-compatible convenience alias.
build-image: build-images

publish-images: build-images
	docker push $(OPENCODE_IMAGE)
	docker push $(CODEX_IMAGE)
	docker push $(CLAUDE_IMAGE)

publish-image: publish-images
