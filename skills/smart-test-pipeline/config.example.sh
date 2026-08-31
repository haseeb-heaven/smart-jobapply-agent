#!/usr/bin/env bash
# Operator-owned configuration. Loaded before built-in defaults; CLI flags win.

MAX_ITERATIONS=10
FIX_AGENT=pi
WAIT_CI=true
CI_TIMEOUT=3600
AGENT_TIMEOUT=1800
VALIDATION_TIMEOUT=3600
REVIEW_BOTS="coderabbit greptile"
TEST_CMD="python -m pytest --tb=short -q"
LINT_CMD=""
PRE_REVIEW_WAIT=30
REVIEW_TIMEOUT=600
POLL_INTERVAL=30

# The pipeline refuses validation if auto/bwrap/sandbox-exec/docker cannot
# provide a disposable boundary.
VALIDATION_SANDBOX=auto
AGENT_SANDBOX=auto

# Supporting files may be changed only when a finding exists and the path is
# in one of these narrowly scoped test patterns.
ALLOWED_SUPPORT_GLOBS="tests/** test/** **/test_*.py **/*_test.*"

# These are names only. Values are copied from the operator environment into
# the model process; GitHub, SSH, cloud, and generic secret variables are not.
PI_PROVIDER_ENV="ANTHROPIC_API_KEY"
CLAUDE_PROVIDER_ENV="ANTHROPIC_API_KEY"
CODEX_PROVIDER_ENV="OPENAI_API_KEY"
OPENCODE_PROVIDER_ENV="OPENAI_API_KEY"
# Docker requires an image with bash and the selected agent/runtime installed.
SANDBOX_IMAGE=""
