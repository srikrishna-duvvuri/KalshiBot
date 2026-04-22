#!/bin/bash
# Outputs the Claude Code OAuth access token from the macOS Keychain.
# Used by `claude --bare --settings '{"apiKeyHelper": "..."}' so the bot can use
# the existing Claude Code account without a separate ANTHROPIC_API_KEY.
ACCOUNT=$(security find-generic-password -s "Claude Code-credentials" 2>/dev/null | grep '"acct"' | awk -F'"' '{print $4}')
security find-generic-password -s "Claude Code-credentials" -a "$ACCOUNT" -w \
  | python3 -c "import json,sys; print(json.loads(sys.stdin.read())['claudeAiOauth']['accessToken'])"
