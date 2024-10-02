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

import sys

# Chooses the right init function
# from importlib.machinery import ModuleSpec
from typing import Optional


# @see importlib.machinery.PathFinder
class _CyPackageMetaPathFinder(abc.MetaPathFinder):
    def __init__(self, name_filter: str, file: str, keep_modules: set):
        """
        自定义 MetaPathFinder, 用于找到和加载 Cython 编译的模块

        Parameters:
        - name_filter : str, ezgl.
        - file : str, __compile__.so 文件路径
        - keep_modules : set, 保持正常加载的模块
        """
        super(_CyPackageMetaPathFinder, self).__init__()
        self._name_filter = name_filter
        self._file = file
        self._keep_modules = keep_modules

    def find_module(self, fullname: str, path: str) -> Optional[importlib.machinery.ExtensionFileLoader]:
        last_name = fullname.split('.')[-1]
        if last_name in self._keep_modules:  # NOTE : 保持正常加载的模块
            return None

        if fullname.startswith(self._name_filter):
            # use this extension-file but PyInit-function of another module:
            return importlib.machinery.ExtensionFileLoader(fullname, self._file)


_registered_prefix = set()


def init(module_name: str, keep_modules: set) -> None:
    """ Load the compiled module, and invoke the PyInit-function of another module """
    module = importlib.import_module(module_name + '.__compile__')
    prefix = module.__name__.split('.', 1)[0] + "."
    for p in _registered_prefix:
        if prefix.startswith(p):
            break
    else:
        _registered_prefix.add(prefix)
        sys.meta_path.append(_CyPackageMetaPathFinder(prefix, module.__file__, keep_modules))
