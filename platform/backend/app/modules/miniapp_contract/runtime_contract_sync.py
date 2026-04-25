from __future__ import annotations

import re


class MiniappRuntimeContractSync:
    _HELPER_ONLY_ROUTE_MODULES = {"role_pages"}

    @staticmethod
    def normalize_future_annotations_import(content: str) -> str:
        updated = str(content or "")
        future_line = "from __future__ import annotations"
        lines = updated.splitlines()
        future_positions = [index for index, line in enumerate(lines) if line.strip() == future_line]
        if not future_positions:
            return updated
        filtered = [line for index, line in enumerate(lines) if index not in future_positions]
        insert_at = 0
        if filtered and filtered[0].startswith("#!"):
            insert_at = 1
        while insert_at < len(filtered) and re.match(r"^#.*coding[:=]", filtered[insert_at]):
            insert_at += 1
        while insert_at < len(filtered) and filtered[insert_at].strip() == "":
            insert_at += 1
        normalized_lines = filtered[:insert_at] + [future_line, ""] + filtered[insert_at:]
        normalized = "\n".join(normalized_lines)
        if updated.endswith("\n"):
            normalized += "\n"
        return normalized

    @staticmethod
    def canonicalize_local_role_links_in_text(content: str) -> str:
        updated = str(content or "")
        role_root_pattern = re.compile(
            r'(?P<quote>["\'`])(?P<path>/(?:client|specialist|manager)(?:/[A-Za-z0-9_{}:-]+)*)/(?P<suffix>(?:[?#][^"\'`]*)?)(?P=quote)'
        )

        def _replace(match: re.Match[str]) -> str:
            quote = match.group("quote")
            path = match.group("path")
            suffix = match.group("suffix") or ""
            return f"{quote}{path}{suffix}{quote}"

        return role_root_pattern.sub(_replace, updated)

    @staticmethod
    def strip_noncanonical_runtime_route_handlers(content: str) -> str:
        lines = str(content or "").splitlines()
        if not lines:
            return str(content or "")
        runtime_decorator = re.compile(r'^\s*@router\.(?:get|post|put|patch|delete)\(["\']/api/runtime/')
        definition_line = re.compile(r'^\s*(?:async\s+)?def\s+[A-Za-z_][A-Za-z0-9_]*\(')
        kept: list[str] = []
        index = 0
        removed_any = False
        while index < len(lines):
            line = lines[index]
            if runtime_decorator.match(line):
                removed_any = True
                while index < len(lines) and runtime_decorator.match(lines[index]):
                    index += 1
                if index < len(lines) and definition_line.match(lines[index]):
                    index += 1
                    while index < len(lines):
                        current = lines[index]
                        if current.strip() == "":
                            index += 1
                            continue
                        if not current.startswith((" ", "\t")):
                            break
                        index += 1
                    continue
            kept.append(line)
            index += 1
        updated = "\n".join(kept)
        if str(content or "").endswith("\n"):
            updated += "\n"
        if removed_any:
            updated = re.sub(r"\n{3,}", "\n\n", updated)
        return updated

    @classmethod
    def normalize_runtime_route_module_source(cls, content: str) -> str:
        updated = cls.normalize_future_annotations_import(content)
        if "/api/runtime/" not in updated:
            return updated
        helper_block = (
            '\n\ndef _normalize_runtime_role(role: str) -> str:\n'
            '    normalized = str(role or "").strip().strip("/")\n'
            '    if normalized == "sample":\n'
            '        return "client"\n'
            '    return normalized\n'
        )
        if "_normalize_runtime_role" not in updated:
            if "ALLOWED_ROLES = {" in updated:
                updated = re.sub(r"(ALLOWED_ROLES\s*=\s*\{[^\n]+\}\n)", r"\1" + helper_block, updated, count=1)
            else:
                updated = helper_block.lstrip("\n") + "\n" + updated
        replacement = (
            "def _validate_role(role: str) -> str:\n"
            "    normalized = _normalize_runtime_role(role)\n"
            "    if normalized not in ALLOWED_ROLES:\n"
            "        raise HTTPException(status_code=404, detail=\"Role not supported\")\n"
            "    return normalized\n"
        )
        updated = cls._replace_top_level_function(updated, "_validate_role", replacement)
        updated = re.sub(r"(?m)^(\s*)_validate_role\(role\)\s*$", r"\1role = _validate_role(role)", updated)
        updated = updated.replace(
            '    raise HTTPException(status_code=404, detail="Action not supported")\n',
            '    return {\n'
            '        "next_path": f"/{role}/",\n'
            '        "message": "No-op runtime action.",\n'
            '        "action_id": action_id,\n'
            '        "role": role,\n'
            '    }\n',
        )
        return updated

    @staticmethod
    def ensure_fastapi_import_symbol(content: str, symbol: str) -> str:
        updated = str(content or "")
        import_pattern = re.compile(r"from fastapi import ([^\n]+)")
        match = import_pattern.search(updated)
        if not match:
            return updated
        symbols = [item.strip() for item in match.group(1).split(",") if item.strip()]
        if symbol in symbols:
            return updated
        symbols.append(symbol)
        ordered_symbols: list[str] = []
        for item in symbols:
            if item not in ordered_symbols:
                ordered_symbols.append(item)
        replacement = f"from fastapi import {', '.join(ordered_symbols)}"
        return import_pattern.sub(replacement, updated, count=1)

    @classmethod
    def _replace_top_level_function(cls, source: str, function_name: str, replacement: str) -> str:
        lines = str(source or "").splitlines()
        if not lines:
            return replacement.rstrip() + "\n"
        pattern = re.compile(rf"^def {re.escape(function_name)}\(")
        start_index: int | None = None
        for index, line in enumerate(lines):
            if pattern.match(line):
                start_index = index
                break
        if start_index is None:
            insert_at = 0
            for index, line in enumerate(lines):
                if line.startswith("@router.") or line.startswith("router =") or line.startswith("def "):
                    insert_at = index
                    break
            new_lines = lines[:insert_at] + replacement.strip("\n").splitlines() + [""] + lines[insert_at:]
            return cls._restore_trailing_newline(source, new_lines)
        end_index = start_index + 1
        while end_index < len(lines):
            current = lines[end_index]
            if current.strip() == "":
                end_index += 1
                continue
            if not current.startswith((" ", "\t")):
                break
            end_index += 1
        new_lines = lines[:start_index] + replacement.strip("\n").splitlines() + lines[end_index:]
        return cls._restore_trailing_newline(source, new_lines)

    @staticmethod
    def _restore_trailing_newline(original: str, lines: list[str]) -> str:
        updated = "\n".join(lines)
        if str(original or "").endswith("\n"):
            updated += "\n"
        return updated


__all__ = ["MiniappRuntimeContractSync"]
