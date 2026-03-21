"""
cypack 运行时导入钩子。

通过注册一个 MetaPathFinder，把子模块导入请求重定向到
已编译生成的 __compile__.so/.pyd 扩展模块。
"""
import importlib
import importlib.abc as abc
import importlib.util
import importlib.machinery
import sys
import os
from typing import Optional, Iterable, Any, Set

_DEBUG = os.environ.get("CYPACK_DEBUG_IMPORT", "0") not in ("0", "false", "False", "")


def _dprint(*a):
    if _DEBUG:
        print("[cypack]", *a)


class _CyPackMetaPathFinder(abc.MetaPathFinder):
    def __init__(self, name_filter: str, file: str, keep_modules: Set[str]):
        """
        Args:
            name_filter: 需要匹配的模块前缀，例如 ``main_module.`` 或
                ``main_module.sub_module.``。
            file: __compile__.so/.pyd 的文件路径。
            keep_modules: 需要跳过 hook、继续走普通导入流程的模块名
                （不带后缀）。
        """
        super().__init__()
        self._name_filter = name_filter
        self._file = file
        self._keep_modules = keep_modules

    def find_spec(
        self,
        fullname: str,
        path: Optional[Iterable[str]] = None,
        target: Any = None,
        *args,
        **kwargs,
    ) -> Optional[importlib.machinery.ModuleSpec]:
        # 当当前 finder 属于 "main_module." 时，
        # 这里会把 "main_module.sub_module.core" 的首段识别成 "sub_module"，
        # 从而让嵌套 bridge 包可以绕过父包 finder，交给自己的 finder 处理。
        suffix = fullname[len(self._name_filter):] if fullname.startswith(self._name_filter) else ""
        first_name = suffix.split(".", 1)[0] if suffix else fullname.split(".")[-1]
        last_name = fullname.split(".")[-1]
        if first_name in self._keep_modules or last_name in self._keep_modules:
            _dprint("find_spec skip keep_module:", fullname)
            return None

        if fullname.startswith(self._name_filter):
            _dprint("find_spec HIT:", fullname, "->", self._file)
            loader = importlib.machinery.ExtensionFileLoader(fullname, self._file)
            spec = importlib.machinery.ModuleSpec(
                name=fullname,
                loader=loader,
                origin=self._file,
            )
            return spec

        return None

    def find_module(
        self,
        fullname: str,
        path: Optional[Iterable[str]] = None,
    ) -> Optional[importlib.machinery.ExtensionFileLoader]:
        spec = self.find_spec(fullname, path)
        if spec is not None:
            return spec.loader
        return None


_registered_prefix: Set[str] = set()


def init(module_name: str, keep_modules: Set[str]) -> None:
    """
    由注入后的 __init__.py 在运行时调用。

    Args:
        module_name: 当前包名，例如 ``main_module`` 或
            ``main_module.sub_module``。
        keep_modules: 需要保留普通导入行为的模块名（不带后缀）。
    """
    _dprint("init() called", "module_name=", module_name, "keep_modules=", keep_modules)

    module = importlib.import_module(module_name + ".__compile__")
    _dprint("imported compile module:", module.__name__, "file:", getattr(module, "__file__", None))

    # 这里直接用 module_name + "." 作为匹配前缀，
    # 而不是像旧实现那样提取顶层包前缀。
    # 这样每个包 / bridge 都能拥有自己独立作用域的 finder。
    prefix = module_name + "."
    _dprint("computed prefix:", prefix)

    # 每个包 / bridge 只注册一个对应前缀的 finder；
    # 通过精确前缀去重，既能避免重复注册，也不会误合并嵌套包的 finder。
    if prefix not in _registered_prefix:
        _registered_prefix.add(prefix)
        finder = _CyPackMetaPathFinder(prefix, module.__file__, keep_modules)
        sys.meta_path.append(finder)
        _dprint("finder appended. meta_path tail=", sys.meta_path[-3:])
