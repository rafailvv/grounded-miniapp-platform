TASK_PROFILES = {
    "openai_code_fast": {
        "label": "OpenAI Code Fast",
        "provider": "openrouter",
        "description": "Default iterative coding profile using GPT-5.3 Codex for code work and GPT-5.4 for general reasoning.",
        "routing": {
            "spec_analysis": "openai/gpt-5.4",
            "ir_codegen": "openai/gpt-5.3-codex",
            "code_plan": "openai/gpt-5.4",
            "code_edit": "openai/gpt-5.3-codex",
            "repair": "openai/gpt-5.3-codex",
            "summarize": "openai/gpt-5.4",
            "cheap_task": "openai/gpt-5.4",
        },
        "default": True,
    },
    "research_balanced": {
        "label": "Research Balanced",
        "provider": "openrouter",
        "description": "Balanced profile using GPT-5.4 for reasoning and GPT-5.3 Codex for implementation.",
        "routing": {
            "spec_analysis": "openai/gpt-5.4",
            "ir_codegen": "openai/gpt-5.3-codex",
            "code_plan": "openai/gpt-5.4",
            "code_edit": "openai/gpt-5.3-codex",
            "repair": "openai/gpt-5.3-codex",
            "summarize": "openai/gpt-5.4",
            "cheap_task": "openai/gpt-5.4",
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
        "fallback": "openai/gpt-5.4",
    },
    "embedding": {
        "primary": "openai/text-embedding-3-large",
        "fallback": "openai/text-embedding-3-large",
    },
}
