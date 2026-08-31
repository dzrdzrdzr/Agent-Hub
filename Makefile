PYTHON ?= python3
VENV ?= .venv

.PHONY: setup test daemon demo extension package clean

setup:
	./scripts/bootstrap.sh

test:
	$(VENV)/bin/python -m pytest agentd/tests -q

daemon:
	AGENT_HUB_CONFIG="$(CURDIR)/config.local.yaml" $(VENV)/bin/agent-hub-daemon

demo:
	AGENT_HUB_CONFIG="$(CURDIR)/config.test.yaml" $(VENV)/bin/agent-hub-daemon

extension:
	cd extension && npm ci && npm run compile

package:
	mkdir -p dist
	cd extension && npm ci && npm run package -- --out ../dist/agent-hub.vsix

clean:
	rm -rf .agent-hub .pytest_cache dist extension/out extension/daemon
