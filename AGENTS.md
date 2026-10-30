# Delegation preference

Use GPT-6 Luna as the default model for delegated agents unless the user explicitly requests a different model. This preference concerns development subagents; changing Prism's runtime model routing is a separate product decision.

Keep research and execution paper-only. Use offline fixtures for development; do not start the installed service, access credentials, or run paid research cycles unless the user asks for those actions.

## Additional delegation policy for Prism in T3 Code

This is a persistent project preference for every current and future Prism development session in T3 Code, including new chats and reopened sessions. It remains in effect until the user changes or removes it; it is not limited to the conversation that created it.

Only when the host-provided session instructions identify this as a T3 Code session working on Prism, read and follow `.t3/AGENTS.md`. This additional delegation policy does not apply in the standalone Codex app, ChatGPT Work, other projects, or sessions outside T3 Code. The presence of these files or T3 tools alone does not establish that the session is hosted by T3 Code.
