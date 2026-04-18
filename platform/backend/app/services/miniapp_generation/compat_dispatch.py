from __future__ import annotations

from typing import Any


class GenerationServiceMeta(type):
    def __getattr__(cls, name: str) -> Any:
        mapping = cls._compat_class_owner_map()
        owner = mapping.get(name)
        if owner is None:
            raise AttributeError(name)
        try:
            return getattr(owner, name)
        except AttributeError:
            if name.startswith("_"):
                return getattr(owner, name[1:])
            raise


class CompatibilityDispatchMixin:
    @classmethod
    def _compat_class_owner_map(cls) -> dict[str, Any]:
        return {}

    @classmethod
    def _compat_instance_owner_map(cls) -> dict[str, Any]:
        return {}

    @classmethod
    def _compat_owner_factories(cls) -> dict[str, Any]:
        return {}

    def _compat_owner(self, owner_ref: Any) -> Any:
        if not isinstance(owner_ref, str):
            return owner_ref
        try:
            return object.__getattribute__(self, owner_ref)
        except AttributeError:
            factory = self._compat_owner_factories().get(owner_ref)
            if factory is None:
                raise
            owner = factory(self)
            setattr(self, owner_ref, owner)
            return owner

    @staticmethod
    def _compat_lookup(owner: Any, name: str) -> Any:
        try:
            return getattr(owner, name)
        except AttributeError:
            if name.startswith("_"):
                return getattr(owner, name[1:])
            raise

    def __getattr__(self, name: str) -> Any:
        instance_mapping = self._compat_instance_owner_map()
        owner_ref = instance_mapping.get(name)
        if owner_ref is not None:
            owner = self._compat_owner(owner_ref)
            return self._compat_lookup(owner, name)
        class_mapping = self._compat_class_owner_map()
        owner = class_mapping.get(name)
        if owner is not None:
            return self._compat_lookup(owner, name)
        raise AttributeError(name)
