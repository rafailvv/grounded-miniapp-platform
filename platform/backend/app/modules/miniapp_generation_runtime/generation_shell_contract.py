from __future__ import annotations

import re

from app.modules.miniapp_generation_runtime.runtime_owner import MiniappGenerationRuntimeOwner
from app.services.miniapp_generation.shell_contract import (
    BASE_STYLESHEET_HREF,
    BASE_STYLESHEET_PATH,
    PAGE_SHELL_CLASS,
    PAGE_SHELL_INLINE_STYLE,
    PREVIEW_BRIDGE_PATH,
    PREVIEW_BRIDGE_SRC,
)


class MiniappGenerationShellContract(MiniappGenerationRuntimeOwner):
    BASE_STYLESHEET_PATH = BASE_STYLESHEET_PATH
    BASE_STYLESHEET_HREF = BASE_STYLESHEET_HREF
    PREVIEW_BRIDGE_PATH = PREVIEW_BRIDGE_PATH
    PREVIEW_BRIDGE_SRC = PREVIEW_BRIDGE_SRC
    PAGE_SHELL_CLASS = PAGE_SHELL_CLASS
    PAGE_SHELL_INLINE_STYLE = PAGE_SHELL_INLINE_STYLE

    @classmethod
    def shared_base_link_tag(cls) -> str:
        return f'<link rel="stylesheet" href="{cls.BASE_STYLESHEET_HREF}" />'

    @classmethod
    def preview_bridge_script_tag(cls) -> str:
        return f'<script src="{cls.PREVIEW_BRIDGE_SRC}" defer></script>'

    @classmethod
    def has_required_shell_refs(cls, html: str) -> bool:
        content = str(html or "")
        return (
            cls.BASE_STYLESHEET_HREF in content
            and cls.PREVIEW_BRIDGE_SRC in content
            and cls.PAGE_SHELL_CLASS in content
            and cls.PAGE_SHELL_INLINE_STYLE in content
        )

    @staticmethod
    def inject_head_tag(html: str, tag: str) -> str:
        if "</head>" in html:
            return html.replace("</head>", f"    {tag}\n</head>", 1)
        return f"{tag}\n{html}"

    @classmethod
    def ensure_base_stylesheet_ref(cls, html: str) -> str:
        if cls.BASE_STYLESHEET_HREF in html:
            return html
        return cls.inject_head_tag(html, cls.shared_base_link_tag())

    @classmethod
    def ensure_preview_bridge_ref(cls, html: str) -> str:
        if cls.PREVIEW_BRIDGE_SRC in html:
            return html
        tag = cls.preview_bridge_script_tag()
        if "</body>" in html:
            return html.replace("</body>", f"    {tag}\n</body>", 1)
        return f"{html}\n{tag}"

    @classmethod
    def ensure_page_shell_contract(cls, html: str) -> str:
        shell_style = cls.PAGE_SHELL_INLINE_STYLE
        body_match = re.search(r"<body(?P<attrs>[^>]*)>", html, flags=re.IGNORECASE)
        if body_match is None:
            return html
        body_start = body_match.end()
        main_match = re.search(r"<main(?P<attrs>[^>]*)>", html[body_start:], flags=re.IGNORECASE)
        if main_match is None:
            return html
        absolute_start = body_start + main_match.start()
        absolute_end = body_start + main_match.end()
        main_open = html[absolute_start:absolute_end]
        attrs_match = re.search(r"<main(?P<attrs>[^>]*)>", main_open, flags=re.IGNORECASE)
        attrs = attrs_match.group("attrs") if attrs_match else ""
        class_match = re.search(r'class=(?P<quote>["\'])(?P<value>.*?)(?P=quote)', attrs, flags=re.IGNORECASE)
        if class_match:
            classes = [item for item in class_match.group("value").split() if item]
            if cls.PAGE_SHELL_CLASS not in classes:
                classes.append(cls.PAGE_SHELL_CLASS)
            updated_attrs = attrs[: class_match.start()] + f' class="{" ".join(classes)}"' + attrs[class_match.end() :]
        else:
            updated_attrs = f'{attrs} class="{cls.PAGE_SHELL_CLASS}"'
        style_match = re.search(r'style=(?P<quote>["\'])(?P<value>.*?)(?P=quote)', updated_attrs, flags=re.IGNORECASE)
        if style_match:
            current_style = style_match.group("value").strip().rstrip(";")
            if cls.PAGE_SHELL_INLINE_STYLE not in current_style:
                merged_style = f"{current_style}; {shell_style}".strip("; ").strip()
                updated_attrs = (
                    updated_attrs[: style_match.start()]
                    + f' style="{merged_style}"'
                    + updated_attrs[style_match.end() :]
                )
        else:
            updated_attrs = f'{updated_attrs} style="{shell_style}"'
        replacement = f"<main{updated_attrs}>"
        return html[:absolute_start] + replacement + html[absolute_end:]
