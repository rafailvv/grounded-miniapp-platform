from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class DynamicToolCapability:
    capability_id: str
    domain: str
    title: str
    description: str
    status: str
    keywords: tuple[str, ...] = ()
    model_tools: tuple[str, ...] = ()
    external_tools: tuple[str, ...] = ()
    requires: tuple[str, ...] = ()
    prompt_hint: str = ""
    dynamic: bool = True
    deferred: bool = True
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_dict(self, *, score: int = 0, reason: str = "") -> dict[str, Any]:
        return {
            "capability_id": self.capability_id,
            "domain": self.domain,
            "title": self.title,
            "description": self.description,
            "status": self.status,
            "score": score,
            "selection_reason": reason,
            "keywords": list(self.keywords),
            "model_tools": list(self.model_tools),
            "external_tools": list(self.external_tools),
            "requires": list(self.requires),
            "prompt_hint": self.prompt_hint,
            "dynamic": self.dynamic,
            "deferred": self.deferred,
            "metadata": dict(self.metadata),
        }


class DynamicToolCatalog:
    """Deferred optional capability discovery for model-facing tool search."""

    _CAPABILITIES: tuple[DynamicToolCapability, ...] = (
        DynamicToolCapability(
            capability_id="browser.verify",
            domain="browser",
            title="Browser verification",
            description="Run a browser workflow proof against the local preview when a UI flow needs end-to-end evidence.",
            status="available_dynamic",
            keywords=("browser", "verify", "preview", "click", "mobile", "console", "screenshot", "playwright", "ui"),
            model_tools=("browser_verify",),
            requires=("preview_runtime", "explicit_verification_phase"),
            prompt_hint="Use only after product code is ready or a browser-specific failure needs proof.",
            deferred=False,
            metadata={"canonical": "browser.verify"},
        ),
        DynamicToolCapability(
            capability_id="deploy.vercel",
            domain="deploy",
            title="Deploy through Vercel",
            description="Prepare or hand off deployment actions for a Vercel project without exposing deploy tools in every model turn.",
            status="deferred_connector_required",
            keywords=("deploy", "deployment", "publish", "preview url", "production", "vercel", "build", "environment"),
            external_tools=("vercel", "vercel_cli", "github_actions"),
            requires=("human_approval", "connector_or_cli_credentials"),
            prompt_hint="Ask for deployment intent and environment before surfacing deploy-specific tools.",
            metadata={"provider": "vercel"},
        ),
        DynamicToolCapability(
            capability_id="database.manage",
            domain="database",
            title="Database management",
            description="Discover database schema, migrations, seed data, and persistence checks only when the product needs storage changes.",
            status="deferred_connector_required",
            keywords=("db", "database", "postgres", "sqlite", "schema", "migration", "seed", "persistence", "sql", "storage"),
            external_tools=("postgres", "neon", "sqlite", "supabase"),
            requires=("workspace_db_config", "migration_policy", "human_approval_for_destructive_changes"),
            prompt_hint="Prefer local schema inspection first; never run destructive DB actions without explicit approval.",
            metadata={"risk": "stateful"},
        ),
        DynamicToolCapability(
            capability_id="payments.stripe",
            domain="payments",
            title="Payments integration",
            description="Stripe/payment setup, webhook, checkout, and subscription workflows surfaced only for payment-related tasks.",
            status="deferred_connector_required",
            keywords=("payment", "payments", "stripe", "checkout", "billing", "subscription", "invoice", "webhook", "pricing"),
            external_tools=("stripe", "vercel_marketplace"),
            requires=("payment_provider_credentials", "webhook_secret", "human_approval"),
            prompt_hint="First generate local integration code and tests; keep live payment actions deferred.",
            metadata={"risk": "financial"},
        ),
        DynamicToolCapability(
            capability_id="cms.content",
            domain="cms",
            title="CMS content integration",
            description="Content model, preview, and CMS connector workflows for Sanity/Contentful/Dato/Storyblok-style products.",
            status="deferred_connector_required",
            keywords=("cms", "content", "sanity", "contentful", "dato", "storyblok", "builder", "preview", "editorial"),
            external_tools=("sanity", "contentful", "dato", "storyblok", "builder"),
            requires=("cms_provider_config", "content_model_policy"),
            prompt_hint="Use when the user asks for content-backed pages or editorial workflows.",
            metadata={"risk": "external_content"},
        ),
        DynamicToolCapability(
            capability_id="github.workflow",
            domain="github",
            title="GitHub workflow",
            description="Issues, PRs, CI, branch publishing, and repository operations exposed only when source control work is requested.",
            status="deferred_connector_required",
            keywords=("github", "pr", "pull request", "issue", "ci", "actions", "branch", "commit", "review", "repository"),
            external_tools=("github_connector", "gh_cli"),
            requires=("github_auth", "repo_scope", "human_approval_for_publish"),
            prompt_hint="Use for PR/CI/source-control tasks; normal code generation should stay on local tools.",
            metadata={"risk": "source_control"},
        ),
        DynamicToolCapability(
            capability_id="vercel.platform",
            domain="vercel",
            title="Vercel platform operations",
            description="Project, environment, deployment, logs, domains, firewall, storage, and workflow operations for Vercel-backed apps.",
            status="deferred_connector_required",
            keywords=("vercel", "env", "logs", "domain", "firewall", "blob", "kv", "deployment", "project", "marketplace"),
            external_tools=("vercel_connector", "vercel_cli"),
            requires=("vercel_auth", "project_scope", "human_approval_for_mutations"),
            prompt_hint="Use after local build state is understood and a Vercel-specific action is needed.",
            metadata={"provider": "vercel"},
        ),
    )

    @classmethod
    def manifest(cls) -> dict[str, Any]:
        domains = sorted({item.domain for item in cls._CAPABILITIES})
        return {
            "schema": "grounded.dynamic_tool_catalog.v1",
            "policy": "deferred_discovery",
            "default_visible_tool": "tool_search",
            "domains": domains,
            "capability_count": len(cls._CAPABILITIES),
            "capabilities": [item.as_dict() for item in cls._CAPABILITIES],
            "rules": [
                "Optional connector/platform tools are not exposed to the model by default.",
                "tool_search returns capability cards and model tools that may be forced in a later turn.",
                "Discovered tools never bypass sandbox, hooks, exec policy, apply policy, or human approval.",
            ],
        }

    @classmethod
    def search(cls, *, query: str = "", domain: str = "", intent: str = "", limit: int = 6) -> dict[str, Any]:
        tokens = cls._tokens(" ".join([query, domain, intent]))
        normalized_domain = str(domain or "").strip().lower()
        scored: list[tuple[int, str, DynamicToolCapability]] = []
        for capability in cls._CAPABILITIES:
            haystack = cls._tokens(" ".join([capability.capability_id, capability.domain, capability.title, capability.description, " ".join(capability.keywords)]))
            score = 0
            if normalized_domain and capability.domain == normalized_domain:
                score += 8
            overlap = tokens & haystack
            score += len(overlap) * 3
            if any(token in capability.domain for token in tokens):
                score += 2
            if not tokens and not normalized_domain:
                score = 1
            if score <= 0:
                continue
            reason = "domain_match" if normalized_domain and capability.domain == normalized_domain else "keyword_overlap" if overlap else "catalog_default"
            scored.append((score, reason, capability))
        scored.sort(key=lambda item: (-item[0], item[2].domain, item[2].capability_id))
        items = [capability.as_dict(score=score, reason=reason) for score, reason, capability in scored[: max(1, min(limit, 12))]]
        return {
            "schema": "grounded.dynamic_tool_search.v1",
            "status": "ready" if items else "empty",
            "query": query,
            "domain": domain,
            "intent": intent,
            "items": items,
            "summary": {
                "matched_count": len(items),
                "available_model_tools": sorted({tool for item in items for tool in item.get("model_tools", [])}),
                "deferred_count": sum(1 for item in items if item.get("deferred")),
                "domains": sorted({str(item.get("domain") or "") for item in items}),
            },
            "activation": {
                "policy": "deferred",
                "next_step": "Use a returned model_tool only when the runtime explicitly forces it into the next tool set; otherwise ask for approval or continue with local code tools.",
            },
        }

    @staticmethod
    def _tokens(text: str) -> set[str]:
        aliases = {
            "db": "database",
            "postgresql": "postgres",
            "pull": "pr",
            "pullrequest": "pr",
            "billing": "payments",
            "payment": "payments",
        }
        tokens = {token for token in re_split_words(text) if len(token) >= 2}
        return {aliases.get(token, token) for token in tokens}


def re_split_words(text: str) -> list[str]:
    import re

    return re.findall(r"[a-z0-9]+", str(text or "").lower())
