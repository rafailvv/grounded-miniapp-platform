TASK_PROFILES = {
    "openai_code_fast": {
        "label": "OpenAI Code Fast",
        "provider": "openrouter",
        "description": "Default iterative coding profile with moderate cost and strong code-edit behavior.",
        "routing": {
            "spec_analysis": "openai/gpt-5-mini",
            "ir_codegen": "openai/gpt-5.1-codex-mini",
            "code_plan": "openai/gpt-5-mini",
            "code_edit": "openai/gpt-5.1-codex-mini",
            "repair": "openai/gpt-5.1-codex-mini",
            "summarize": "openai/gpt-5-mini",
            "cheap_task": "openai/gpt-5-mini",
        },
        "default": True,
    },
    "research_balanced": {
        "label": "Research Balanced",
        "provider": "openrouter",
        "description": "Balanced profile for grounded artifact generation with stronger reasoning steps.",
        "routing": {
            "spec_analysis": "openai/gpt-5-mini",
            "ir_codegen": "openai/gpt-5-mini",
            "code_plan": "openai/gpt-5-mini",
            "code_edit": "openai/gpt-5-mini",
            "repair": "openai/gpt-5-mini",
            "summarize": "openai/gpt-5-mini",
            "cheap_task": "openai/gpt-5-mini",
        },
        "default": False,
    },
}

MODEL_REGISTRY = {
    "spec_analysis": {
        "primary": TASK_PROFILES["openai_code_fast"]["routing"]["spec_analysis"],
        "fallback": TASK_PROFILES["research_balanced"]["routing"]["spec_analysis"],
    },
    "ir_codegen": {
        "primary": TASK_PROFILES["openai_code_fast"]["routing"]["ir_codegen"],
        "fallback": TASK_PROFILES["research_balanced"]["routing"]["ir_codegen"],
    },
    "code_plan": {
        "primary": TASK_PROFILES["openai_code_fast"]["routing"]["code_plan"],
        "fallback": TASK_PROFILES["research_balanced"]["routing"]["code_plan"],
    },
    "code_edit": {
        "primary": TASK_PROFILES["openai_code_fast"]["routing"]["code_edit"],
        "fallback": TASK_PROFILES["research_balanced"]["routing"]["code_edit"],
    },
    "repair": {
        "primary": TASK_PROFILES["openai_code_fast"]["routing"]["repair"],
        "fallback": TASK_PROFILES["research_balanced"]["routing"]["repair"],
    },
    "summarize": {
        "primary": TASK_PROFILES["openai_code_fast"]["routing"]["summarize"],
        "fallback": TASK_PROFILES["research_balanced"]["routing"]["summarize"],
    },
    "cheap_task": {
        "primary": TASK_PROFILES["openai_code_fast"]["routing"]["cheap_task"],
        "fallback": "openai/gpt-5-mini",
    },
    "embedding": {
        "primary": "openai/text-embedding-3-large",
        "fallback": "openai/text-embedding-3-large",
    },
}
