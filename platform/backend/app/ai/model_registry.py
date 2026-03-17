MODEL_REGISTRY = {
    "spec_analysis": {
        "primary": "openai/o4-mini",
        "fallback": "openai/o4-mini",
    },
    "ir_codegen": {
        "primary": "openai/o4-mini",
        "fallback": "openai/o4-mini",
    },
    "repair": {
        "primary": "openai/o4-mini",
        "fallback": "openai/o4-mini",
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
