from __future__ import annotations

from typing import Any


class GenerationServiceMeta(type):
    def __getattr__(cls, name: str) -> Any:
        mapping = cls._compat_class_owner_map()
        owner = mapping.get(name)
        if owner is None:
            raise AttributeError(name)
        return getattr(owner, name)


class CompatibilityDispatchMixin:
    @classmethod
    def _compat_class_owner_map(cls) -> dict[str, Any]:
        return {}

    @classmethod
    def _compat_instance_owner_map(cls) -> dict[str, Any]:
        return {}

    def __getattr__(self, name: str) -> Any:
        instance_mapping = self._compat_instance_owner_map()
        owner_ref = instance_mapping.get(name)
        if owner_ref is not None:
            owner = getattr(self, owner_ref) if isinstance(owner_ref, str) else owner_ref
            return getattr(owner, name)
        class_mapping = self._compat_class_owner_map()
        owner = class_mapping.get(name)
        if owner is not None:
            return getattr(owner, name)
        raise AttributeError(name)
