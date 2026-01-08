# cypack/build.py
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
import io
import itertools
import os
import warnings
from glob import glob
from pathlib import Path
from pprint import pprint
from typing import List, Tuple, Dict, Any, Union

from Cython.Build import cythonize
from setuptools import Extension
from setuptools.command.build_py import build_py as original_build_py

_conf = {
    "ext_modules": True,
    "inject_init": True,
    "remove_source": True,
    "compile_py": True,
    "optimize": 1,
    "exclude": [],       # 不参与编译的 modules（glob pattern）
    "keep_modules": [],  # 保留而不编译的 modules（文件名，含扩展名）
}


def _compile_packages(conf: Dict[str, Any], packages: List[str]) -> List[Extension]:
    """
    查找每个包中的 __compile__.py 文件，并收集所有 .pyx 和 .py 文件,
    排除 __init__.py / __compile__.py / exclude_files / keep_modules，
    生成一个名为 package.__compile__ 的 Extension 对象。
    """
    extensions: List[Extension] = []
    exclude_files = set(itertools.chain.from_iterable([glob(g) for g in conf["exclude"]]))
    exclude_files = [str(Path(f)) for f in exclude_files]

    if not packages:
        return extensions

    for package in packages:
        root = Path(package, "__compile__.py")
        if root.exists():
            package_path = str(root.parent)
            sourcefiles = [str(path) for path in Path(package_path).rglob("*.pyx")]

            sourcefiles += [
                str(path)
                for path in Path(package_path).rglob("*.py")
                if path.name not in ["__init__.py", "__compile__.py"]
            ]
            sourcefiles.append(package_path + "/__compile__.py")

            # 过滤 exclude_files 和 keep_modules
            sourcefiles = [
                f
                for f in sourcefiles
                if f not in exclude_files and Path(f).name not in conf["keep_modules"]
            ]

            if sourcefiles:
                extensions.append(
                    Extension(
                        name=f"{package_path}.__compile__",
                        sources=sourcefiles,
                        optional=True,
                    )
                )
    return extensions


class _build_py(original_build_py):
    """ build_py to remove the source file for compiled package """

    def finalize_options(self):
        super().finalize_options()
        conf = _conf
        self.compile = conf["compile_py"]  # Force the pre-compiled python
        self.optimize = conf["optimize"] if self.compile else 0

        self.remove_source = conf["remove_source"]
        self.keep_modules = conf["keep_modules"]
        self.inject_init = conf["inject_init"]
        self._exclude = set(itertools.chain.from_iterable([glob(g) for g in conf["exclude"]]))
        self._patched_init: List[str] = []

        if os.environ.get("CYPACK_DEBUG", "False").lower() == "true":
            print(
                f"=====> {self.compile=} {self.optimize=} "
                f"{self.remove_source=} {self.inject_init=} {self._exclude=}"
            )

    def find_package_modules(self, package: str, package_dir: str) -> List[Tuple[str, str, str]]:
        """ Remove source code """
        modules: List[Tuple[str, str, str]] = super().find_package_modules(package, package_dir)
        if not self.remove_source:
            return modules

        filtered_modules: List[Tuple[str, str, str]] = []
        for (pkg, mod, filepath) in modules:
            _path = Path(filepath)
            if (
                _path.suffix in [".py", ".pyx"]
                and _path.name != "__init__.py"
                and _path.name not in self.keep_modules  # 保留 keep_modules 的源代码
            ):
                continue
            filtered_modules.append((pkg, mod, filepath))
        return filtered_modules

    def find_data_files(self, package, src_dir):
        """ Return filenames for package's data files in 'src_dir'"""
        data_files = super().find_data_files(package, src_dir)
        if not self.remove_source:
            return data_files

        filtered_datas: List[str] = []
        for filepath in data_files:
            _path = Path(filepath)
            if (
                _path.suffix in [".c", ".py", ".pyx"]
                and Path(_path.parent, "__compile__.py").exists()
            ):
                continue
            filtered_datas.append(filepath)
        return filtered_datas

    def build_module(self, module, module_file, package) -> Tuple[str, int]:
        """ Inject init() in __init__ files"""
        outfile, copied = super().build_module(module, module_file, package)

        if not self.inject_init:
            return outfile, copied

        if not outfile.endswith("__init__.py"):
            return outfile, copied

        package_file = package.replace(".", "/")
        if not Path(package_file, "__compile__.py").exists():
            return outfile, copied

        inject = (
            "import cypack; "
            f"cypack.init(__name__, set({[m.split('.')[0] for m in self.keep_modules]}));\n"
        )

        # Patch the __init__.py inject the initialisation
        with io.open(outfile, encoding="utf-8-sig") as f:
            lines = f.readlines()

        update = False
        for i, line in enumerate(lines):
            if line.strip().startswith("#"):
                continue
            if line.startswith("import cypack;") and "cypack.init(" in line:
                lines[i] = inject
            else:
                lines.insert(i, inject)
            update = True
            break
        else:
            lines.append(inject)
            update = True

        if update:
            with io.open(outfile, "w", encoding="utf-8-sig") as f:
                f.writelines(lines)

        return outfile, copied


def build_cypack(setup: Dict[str, Any], conf: Union[bool, Dict[str, Any]] = True):
    """ Plugin for setuptools """
    global _conf
    if not conf:
        return
    if os.environ.get("CYPACK", "True").lower() in ["0", "false"]:
        warnings.warn("Ignore cypack")
        return

    if isinstance(conf, dict):
        _conf = {**_conf, **conf}

    if _conf["ext_modules"]:
        packages = setup.get("packages", [])
        ext_modules = setup.get("ext_modules", [])
        if not packages:
            warnings.warn("Not packages found")
        else:
            compiled_module = cythonize(
                _compile_packages(_conf, packages),
                compiler_directives={"language_level": 3},
                build_dir="build/cypack",
            )
        if ext_modules:
            ext_modules.extend(compiled_module)
        else:
            setup["ext_modules"] = compiled_module

    # Extend the build process to remove the compiled source code
    cmdclass = setup.get("cmdclass", {})
    cmdclass["build_py"] = _build_py
    setup["cmdclass"] = cmdclass

    if "CFLAGS" not in os.environ:
        os.environ["CFLAGS"] = "-O3"

    if os.environ.get("CYPACK_DEBUG", "False").lower() == "true":
        print("=====> ", end="")
        pprint(setup)


def cypack(setup, attr, value: Union[bool, Dict[str, Any]]):
    """setup(cypack=...) 的入口"""
    build_cypack(vars(setup), value)