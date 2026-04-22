from __future__ import annotations

import importlib
import importlib.abc
import importlib.util
import sys

from ibkr_api.compat.aliases import COMPAT_MODULE_ALIASES


class _CompatAliasLoader(importlib.abc.Loader):
    def __init__(self, alias_name: str, target_name: str):
        self.alias_name = alias_name
        self.target_name = target_name

    def create_module(self, spec):
        module = importlib.import_module(self.target_name)
        sys.modules[self.alias_name] = module
        parent_name, _, child_name = self.alias_name.rpartition(".")
        parent_module = sys.modules.get(parent_name)
        if parent_module is not None:
            setattr(parent_module, child_name, module)
        return module

    def exec_module(self, module):
        return None


class _CompatAliasFinder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        target_name = COMPAT_MODULE_ALIASES.get(fullname)
        if not target_name:
            return None
        return importlib.util.spec_from_loader(
            fullname,
            _CompatAliasLoader(fullname, target_name),
            origin=f"compat-alias:{target_name}",
        )


def register_compat_alias_finder():
    for finder in sys.meta_path:
        if isinstance(finder, _CompatAliasFinder):
            return
    sys.meta_path.insert(0, _CompatAliasFinder())


__all__ = [
    "register_compat_alias_finder",
]
