# cypack/init.py
"""
   Copyright 2021 Philippe PRADOS

   Licensed under the Apache License, Version 2.0 (the "License");
   you may not use this file except in compliance with the License.
   You may obtain a copy of the License at

       http://www.apache.org/licenses/LICENSE-2.0

   Unless required by applicable law or agreed to in writing, software
   distributed under the License is distributed on an "AS IS" BASIS,
   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
   See the License for the specific language governing permissions and
   limitations under the License.
"""
import importlib
import importlib.abc as abc  # Never remove this import, else the importlib.abc can not be visible with Python 3.10
import importlib.util
import importlib.machinery
import sys
from typing import Optional, Iterable, Any, Set
import os

_DEBUG = os.environ.get("CYPACK_DEBUG_IMPORT", "0") not in ("0", "false", "False", "")
def _dprint(*a):
    if _DEBUG:
        print("[cypack]", *a)

class _CyPackMetaPathFinder(abc.MetaPathFinder):
    def __init__(self, name_filter: str, file: str, keep_modules: Set[str]):
        """
        自定义 MetaPathFinder, 用于找到和加载 Cython 编译的模块

        Parameters:
        - name_filter : str, 比如 "xensesdk."
        - file        : str, __compile__.so / .pyd 文件路径
        - keep_modules: set, 保持正常加载的模块（文件名不带后缀）
        """
        super().__init__()
        self._name_filter = name_filter
        self._file = file
        self._keep_modules = keep_modules

    # 注意：签名必须兼容 Python 3.12
    def find_spec(
        self,
        fullname: str,
        path: Optional[Iterable[str]] = None,
        target: Any = None,
        *args,
        **kwargs,
    ) -> Optional[importlib.machinery.ModuleSpec]:
        """
        fullname: 完整模块名，如 'xensesdk.xenseInterface.sensorEnum'
        path    : 一般为父包的 __path__，或 None
        """
        last_name = fullname.split(".")[-1]
        if last_name in self._keep_modules:
            # 保持正常加载的模块（不由 cypack 接管）
            _dprint("222222\nfind_spec skip keep_module:", fullname)
            return None

        _dprint("222222\nfind_spec HIT:", fullname, "->", self._file)
        if fullname.startswith(self._name_filter):
            # 为所有满足前缀的模块提供同一个扩展文件的 spec
            loader = importlib.machinery.ExtensionFileLoader(fullname, self._file)
            spec = importlib.machinery.ModuleSpec(
                name=fullname,
                loader=loader,
                origin=self._file,
            )
            return spec

        return None

    # 兼容极老的 import 逻辑；3.12 仍会优先调用 find_spec
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
    在运行时由 package.__init__ 注入调用。

    module_name : 包名，比如 'xensesdk'
    keep_modules: 需要保持普通导入行为的模块名（文件名不带后缀）
    """
    _dprint("111111\ninit() called", "module_name=", module_name, "keep_modules=", keep_modules)
    # 先导入 {package}.__compile__ 扩展
    module = importlib.import_module(module_name + ".__compile__")
    _dprint("111111\nimported compile module:", module.__name__, "file:", getattr(module, "__file__", None))

    # 取出顶级包前缀，比如 "xensesdk."
    prefix = module.__name__.split(".", 1)[0] + "."
    _dprint("111111\ncomputed prefix:", prefix)

    # 避免重复注册
    for p in _registered_prefix:
        if prefix.startswith(p):
            _dprint("111111\nprefix already registered by", p, "skip")
            break
    else:
        _registered_prefix.add(prefix)
        finder = _CyPackMetaPathFinder(prefix, module.__file__, keep_modules)
        sys.meta_path.append(finder)
        _dprint("111111\nfinder appended. meta_path tail=", sys.meta_path[-3:])