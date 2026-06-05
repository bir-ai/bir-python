# LangChain Demo

This example directory is a placeholder. The current local MVP does not yet
ship a LangChain integration or a LangChain-specific callback handler.

Use `../openai-demo` for the supported dependency-free tracing demo. Until a
dedicated integration lands, LangChain applications can still record useful
local traces by wrapping the application entrypoint with `@observe()` and
placing explicit `span()`, `generation()`, `retrieval()`, and `score()` calls
around the relevant chain steps.
