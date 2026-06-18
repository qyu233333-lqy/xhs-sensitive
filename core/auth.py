"""Compatibility wrapper for the localized auth module."""

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path


_MODULE_PATH = Path(__file__).with_name("auth钉钉登录和“用户 -> 部门.py")
_SPEC = spec_from_file_location("core._auth_localized", _MODULE_PATH)
_MODULE = module_from_spec(_SPEC)
assert _SPEC and _SPEC.loader
_SPEC.loader.exec_module(_MODULE)

for _name, _value in vars(_MODULE).items():
    if not _name.startswith("_"):
        globals()[_name] = _value

