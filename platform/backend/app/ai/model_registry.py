MODEL_REGISTRY = {
    "spec_analysis": {
        "primary": "anthropic/claude-opus-4.6",
        "fallback": "anthropic/claude-sonnet-4.6",
    },
    "ir_codegen": {
        "primary": "openai/gpt-5.3-codex",
        "fallback": "openai/gpt-5.4",
    },
    "repair": {
        "primary": "openai/gpt-5.3-codex",
        "fallback": "openai/gpt-5.4",
    },
    "cheap_task": {
        "primary": "openai/o4-mini",
        "fallback": "anthropic/claude-sonnet-4.6",
    },
    "embedding": {
        "primary": "openai/text-embedding-3-large",
        "fallback": "openai/text-embedding-3-large",
    },
}

