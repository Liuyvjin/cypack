import os
import shutil
import warnings
from fnmatch import fnmatch
from pathlib import Path
from pprint import pprint
from typing import Any, Dict, Iterable, List, Optional, Set

from Cython.Build import cythonize
from setuptools import Extension
from setuptools._distutils import log as distutils_log
from setuptools.command.build_ext import build_ext as original_build_ext
from setuptools.command.build_py import build_py as original_build_py

from .log import get_logger


_LOG = get_logger()
_PYTHON_SOURCE_SUFFIXES = {".py", ".pyx"}
_SKIP_SOURCE_NAMES = {"__init__.py", "__compile__.py"}

# cypack 的全局默认构建配置。
# 这些值可通过 build_cypack(setup_dict, conf={...}) 局部覆盖。
_conf: Dict[str, Any] = {
    # 是否为命中的包生成 ext_modules（即 __compile__.so/.pyd）
    "ext_modules": True,
    # 是否在最终打包出的 __init__.py 中注入 cypack.init()
    "inject_init": True,
    # 是否从最终产物里移除已编译进 __compile__ 的 .py 源文件
    "remove_source": True,
    # 交给 build_py 时是否按“已编译 Python 模块”模式处理
    "compile_py": True,
    # build_py 使用的优化级别；compile_py=False 时会退回 0
    "optimize": 1,
    # 不参与编译、也不进入最终包的相对路径 / glob 规则。
    # 路径永远相对于“当前编译包的源码根目录”来写：
    # - 普通包：相对于包目录本身，例如 src/main_module/__compile__.py
    #   中写 "examples/demo.py"，对应 src/main_module/examples/demo.py
    # - bridge 包：相对于 vendor_path 指向的真实源码目录来写
    #   例如写 "nested/excluded.py"
    "exclude": [],
    # 保留原始 .py 文件、不编译进 __compile__ 的模块 / 相对路径。
    # 同样相对于“当前编译包的源码根目录”来写，支持 glob，
    # 例如 "examples/*.py"、"config.py"、"nested/keep_mod.py"。
    "keep_modules": [],
}

_DEV_BRIDGE_START = "# CYPACK-DEV-BEGIN"
_DEV_BRIDGE_END = "# CYPACK-DEV-END"


def _resolve_package_path(package: str, package_dir: Dict[str, str]) -> Path:
    """
    把 setuptools 的包名解析成真实源码目录，兼容 src/ 布局。

    Args:
        package: 形如 ``a.b.c`` 的包名。
        package_dir: setup.py 中传给 setuptools 的 package_dir 映射。
    """
    package_dir = package_dir or {}
    parts = package.split(".")
    for i in range(len(parts), -1, -1):
        prefix = ".".join(parts[:i])
        if prefix in package_dir:
            base = Path(package_dir[prefix])
            rest = Path(*parts[i:]) if i < len(parts) else Path()
            return base / rest
    if "" in package_dir:
        return Path(package_dir[""]) / Path(*parts)
    return Path(*parts)


def _load_compile_config(compile_path: Path) -> Dict[str, Any]:
    """
    读取 __compile__.py 中 cypack 关心的少量配置。

    Args:
        compile_path: 包目录下的 __compile__.py 路径。

    配置规则补充：
        - exclude / keep_modules 都相对于当前编译包的源码根目录
        - 普通包时，源码根目录就是当前包目录
        - bridge 包时，源码根目录是 vendor_path 指向的目录
    """
    namespace: Dict[str, Any] = {}
    exec(compile_path.read_text(encoding="utf-8"), namespace)
    return {
        "exclude": list(namespace.get("exclude", [])),
        "keep_modules": list(namespace.get("keep_modules", [])),
        "vendor_path": namespace.get("vendor_path"),
    }


def _normalize_rel_path(value: str) -> str:
    """
    把 keep/exclude 写法统一成相对源码根的正斜杠路径。

    Args:
        value: 配置里写入的模块名、相对路径或文件名。

    例如：
        - "config.py" -> "config.py"
        - "examples.demo" -> "examples/demo.py"
        - "examples/*.py" -> "examples/*.py"
    """
    value = value.replace("\\", "/").strip("/")
    if value.endswith(".py"):
        return value
    if "." in value and "/" not in value:
        return value.replace(".", "/") + ".py"
    return value


def _normalize_rel_paths(values: Iterable[str]) -> Set[str]:
    """批量归一化相对路径。"""
    return {_normalize_rel_path(value) for value in values}


def _has_glob_pattern(value: str) -> bool:
    """判断 keep/exclude 条目里是否包含 glob 通配符。"""
    return any(token in value for token in ("*", "?", "["))


def _resolve_keep_paths(source_root: Path, keep_modules: Iterable[str]) -> Set[str]:
    """
    把 keep_modules 解析成源码根目录下的实际相对路径集合。

    支持两种写法：
    - 精确路径：``examples/example_launcher.py``
    - glob：``examples/*.py``

    注意：
        这里的路径始终相对于 source_root，而不是项目根目录。
    """
    resolved: Set[str] = set()
    normalized_items = [_normalize_rel_path(item) for item in keep_modules]

    for item in normalized_items:
        if not _has_glob_pattern(item):
            resolved.add(item)
            continue

        for path in source_root.rglob("*"):
            if not path.is_file():
                continue
            rel_path = path.relative_to(source_root).as_posix()
            if _match_patterns(rel_path, [item]):
                resolved.add(rel_path)

    return resolved


def _compile_skip_roots(keep_modules: Iterable[str]) -> List[str]:
    """
    提取需要绕过 import hook 的顶层模块名。

    Args:
        keep_modules: keep_modules 原始配置。
    """
    roots: List[str] = []
    for item in keep_modules:
        normalized = _normalize_rel_path(item)
        root = normalized.split("/", 1)[0]
        if root.endswith(".py"):
            root = root[:-3]
        if root and root not in roots:
            roots.append(root)
    return roots


def _unique_extend(items: List[str], new_items: Iterable[str]) -> List[str]:
    """按顺序追加去重后的条目。"""
    for item in new_items:
        if item and item not in items:
            items.append(item)
    return items


def _match_patterns(rel_path: str, patterns: Iterable[str]) -> bool:
    """
    判断相对路径是否命中模式规则。

    rel_path 与 patterns 都应当是“相对于当前源码根目录”的路径。
    """
    normalized_path = rel_path.replace("\\", "/")
    return any(fnmatch(normalized_path, pattern.replace("\\", "/")) for pattern in patterns)


def _is_child_compile_path(rel_path: str, child_roots: Iterable[Path]) -> bool:
    """判断当前文件是否位于子编译包目录内。"""
    rel = Path(rel_path)
    return any(rel == child_root or child_root in rel.parents for child_root in child_roots)


def _is_vendor_source(rel_path: str) -> bool:
    """判断路径是否位于 _vendor 目录内。"""
    return "_vendor" in Path(rel_path).parts


def _copy_if_exists(src: Path, dst: Path) -> None:
    """源文件存在时复制到目标位置。"""
    if not src.exists():
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)


def _normalize_build_source_path(path: Path) -> str:
    """
    规范化传给 Cython 的源码路径。

    优先返回“相对于当前构建工作目录”的相对路径，避免 bridge 包因为用了
    绝对 vendor_path 而在 ``build/cypack`` 下生成一长串 ``workspace/...`` 目录。
    """
    cwd = Path.cwd().resolve()
    resolved = path.resolve()
    try:
        return resolved.relative_to(cwd).as_posix()
    except ValueError:
        return str(path)


def _package_output_dir(build_lib: str, package: str) -> Path:
    """返回包在 build_lib 下的输出目录。"""
    return Path(build_lib, *package.split("."))


def _restore_log_threshold(previous: int) -> None:
    """恢复 distutils 日志级别。"""
    distutils_log.set_threshold(previous)


def _short_build_message(cmd: List[str]) -> Optional[str]:
    """把编译命令压缩为更短的提示文本。"""
    if not cmd:
        return None

    executable = Path(cmd[0]).name.lower()
    if executable == "cl.exe":
        for item in cmd[1:]:
            if item.startswith("/Tc") or item.startswith("/Tp"):
                return f"compile: {item[3:]}"
    if executable == "link.exe":
        for item in cmd[1:]:
            if item.startswith("/OUT:"):
                return f"link: {item[5:]}"
    return None


def _inject_init_code(content: str, package: str, keep_roots: List[str]) -> str:
    """
    向构建产物里的 __init__.py 注入 cypack.init()。

    Args:
        content: 原始 __init__.py 内容。
        package: 当前包名，仅用于语义对应。
        keep_roots: 需要绕过 import hook 的顶层模块名。
    """
    del package
    inject = f"import cypack; cypack.init(__name__, set({keep_roots!r}))\n"
    lines = content.splitlines(keepends=True)

    for index, line in enumerate(lines):
        if line.startswith("import cypack;") and "cypack.init(" in line:
            lines[index] = inject
            return "".join(lines)
        if line.strip().startswith("#"):
            continue
        lines.insert(index, inject)
        return "".join(lines)

    lines.append(inject)
    return "".join(lines)


def _strip_dev_bridge(content: str) -> str:
    """
    移除 bridge 包里仅开发态使用的代理代码块。

    Args:
        content: 构建前 bridge __init__.py 的文本内容。
    """
    lines = content.splitlines(keepends=True)
    result: List[str] = []
    skipping = False

    for line in lines:
        if line.strip() == _DEV_BRIDGE_START:
            skipping = True
            continue
        if line.strip() == _DEV_BRIDGE_END:
            skipping = False
            continue
        if not skipping:
            result.append(line)

    return "".join(result)


def _collect_compile_sources(source_root: Path, conf: Dict[str, Any]) -> List[str]:
    """
    收集应被编进单个包级 __compile__ 扩展的源码文件。

    Args:
        source_root: 实际收集源码的根目录；普通包是包目录，bridge 包是 vendor_path。
        conf: 当前包的合并后配置。
    """
    keep_paths = _resolve_keep_paths(source_root, conf["keep_modules"])
    child_roots = [Path(item) for item in conf.get("child_compile_roots", [])]
    sources: List[str] = []

    for path in sorted(source_root.rglob("*")):
        if not path.is_file() or path.suffix not in _PYTHON_SOURCE_SUFFIXES:
            continue

        rel_path = path.relative_to(source_root).as_posix()

        # bridge 构建会把 vendor 源码“吸收”进 bridge 包，因此最终产物
        # 不应该再保留 _vendor 原始源码树。
        if _is_vendor_source(rel_path):
            continue

        # 父包编译时必须跳过子编译包目录，否则父子两个 __compile__
        # 会同时尝试接管同一批模块。
        if _is_child_compile_path(rel_path, child_roots):
            continue

        if path.name in _SKIP_SOURCE_NAMES:
            if path.name == "__compile__.py":
                _LOG.debug("ignore vendor compile marker: %s", path)
            continue

        if _match_patterns(rel_path, conf["exclude"]):
            _LOG.debug("exclude source: %s", rel_path)
            continue

        if rel_path in keep_paths:
            _LOG.debug("keep source: %s", rel_path)
            continue

        sources.append(_normalize_build_source_path(path))

    return sources


def _build_package_configs(setup_dict: Dict[str, Any], conf: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """
    根据 setup() 与各包的 __compile__.py 生成逐包构建配置。

    Args:
        setup_dict: setup() 使用的 kwargs 字典。
        conf: cypack 的全局默认/覆盖配置。
    """
    package_dir = setup_dict.get("package_dir") or {}
    configs: Dict[str, Dict[str, Any]] = {}

    for package in setup_dict.get("packages", []):
        package_path = _resolve_package_path(package, package_dir)
        if "_vendor" in package_path.parts:
            continue

        compile_path = package_path / "__compile__.py"
        if not compile_path.exists():
            continue

        local_conf = _load_compile_config(compile_path)
        package_conf = {
            **conf,
            "exclude": list(conf["exclude"]) + list(local_conf["exclude"]),
            "keep_modules": list(conf["keep_modules"]) + list(local_conf["keep_modules"]),
            "vendor_path": local_conf["vendor_path"],
            "package": package,
            "package_path": package_path,
            "compile_path": compile_path,
        }

        if local_conf["vendor_path"]:
            # bridge 包：实际编译 vendor_path 指向的源码，但产物名仍然挂在
            # bridge 包名下，例如 main_module.sub_module.__compile__。
            source_root = (package_path / local_conf["vendor_path"]).resolve()
            package_conf["is_bridge"] = True
            package_conf["source_root"] = source_root
        else:
            package_conf["is_bridge"] = False
            package_conf["source_root"] = package_path

        package_conf["keep_roots"] = _compile_skip_roots(package_conf["keep_modules"])
        configs[package] = package_conf
        _LOG.info(
            "compile package discovered: %s (%s)",
            package,
            "bridge" if package_conf["is_bridge"] else "package",
        )

    for package, package_conf in configs.items():
        package_root = package_conf["package_path"]

        # 如果 a 和 a.b 都会被编译，那么 a 必须把 b 视作 keep root，
        # 这样子包导入才会下沉到 a.b 自己的 finder，而不是被父包劫持。
        package_conf["child_compile_roots"] = [
            other_conf["package_path"].relative_to(package_root).as_posix()
            for other_package, other_conf in configs.items()
            if other_package != package and other_conf["package_path"].is_relative_to(package_root)
        ]
        child_keep_roots = [
            other_package[len(package) + 1 :].split(".", 1)[0]
            for other_package in configs
            if other_package.startswith(package + ".")
        ]
        package_conf["keep_roots"] = _unique_extend(package_conf["keep_roots"], child_keep_roots)

    return configs


def _compile_packages(configs: Dict[str, Dict[str, Any]]) -> List[Extension]:
    """
    根据逐包配置生成 setuptools Extension 列表。

    Args:
        configs: _build_package_configs 生成的逐包配置。
    """
    extensions: List[Extension] = []

    for package, package_conf in configs.items():
        sources = _collect_compile_sources(package_conf["source_root"], package_conf)
        sources.append(_normalize_build_source_path(package_conf["compile_path"]))
        if not sources:
            continue

        _LOG.info("collect %s source files for %s", len(sources), package)
        extensions.append(
            Extension(
                name=f"{package}.__compile__",
                sources=sources,
                optional=True,
            )
        )

    return extensions


def _find_owner_package_config(package: str, configs: Dict[str, Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """
    找到当前包所属的“最近编译包”配置。

    例如：
    - retiles.core -> retiles
    - main_module.sub_module.nested -> main_module.sub_module
    """
    matches = [
        conf
        for name, conf in configs.items()
        if package == name or package.startswith(name + ".")
    ]
    if not matches:
        return None
    return max(matches, key=lambda conf: len(conf["package"]))


class _build_py(original_build_py):
    """自定义 build_py：剔除源码、保留 keep 文件，并改写 __init__.py。"""

    def finalize_options(self) -> None:
        super().finalize_options()
        self.compile = _conf["compile_py"]
        self.optimize = _conf["optimize"] if self.compile else 0
        self.remove_source = _conf["remove_source"]
        self.inject_init = _conf["inject_init"]
        self.package_configs = _build_package_configs(vars(self.distribution), _conf)
        self._generated_outputs: List[str] = []

    def find_package_modules(self, package: str, package_dir: str):
        """
        仅保留 __init__.py 与显式声明的 keep_modules 作为原始 .py 文件。

        Args:
            package: 当前包名。
            package_dir: setuptools 传入的包目录。
        """
        modules = super().find_package_modules(package, package_dir)
        if not self.remove_source:
            return modules

        package_conf = _find_owner_package_config(package, self.package_configs)
        if not package_conf:
            return modules

        keep_paths = _resolve_keep_paths(package_conf["package_path"], package_conf["keep_modules"])
        filtered = []

        for pkg, mod, filepath in modules:
            path = Path(filepath)
            rel_path = path.relative_to(package_conf["package_path"]).as_posix()

            # 编译产物中不保留源码态 _vendor 目录；bridge 会把真正需要的内容
            # 映射回自己的包目录。
            if _is_vendor_source(rel_path):
                continue

            # __init__.py 默认始终保留，便于在各层包目录中写初始化逻辑。
            if path.name == "__init__.py":
                filtered.append((pkg, mod, filepath))
                continue

            if rel_path in keep_paths:
                filtered.append((pkg, mod, filepath))

        return filtered

    def find_data_files(self, package, src_dir):
        """过滤掉不该进入最终产物的数据文件。"""
        files = super().find_data_files(package, src_dir)
        package_conf = _find_owner_package_config(package, self.package_configs)
        if not self.remove_source or not package_conf:
            return files

        filtered = []
        for filepath in files:
            path = Path(filepath)
            try:
                rel_path = path.relative_to(package_conf["package_path"]).as_posix()
            except ValueError:
                rel_path = path.as_posix()

            if _is_vendor_source(rel_path):
                continue
            if path.suffix in {".py", ".pyx", ".c"}:
                continue
            filtered.append(filepath)
        return filtered

    def build_module(self, module, module_file, package):
        """改写构建产物中的 __init__.py。"""
        outfile, copied = super().build_module(module, module_file, package)
        if not self.inject_init or not outfile.endswith("__init__.py"):
            return outfile, copied

        package_conf = self.package_configs.get(package)
        if not package_conf:
            return outfile, copied

        content = Path(outfile).read_text(encoding="utf-8-sig")
        if package_conf["is_bridge"]:
            # bridge 的 __init__.py 在开发态会指向 _vendor；但构建后的 wheel
            # 不应再依赖这段逻辑，而是改由 cypack.init() 接管导入。
            content = _strip_dev_bridge(content)

        content = _inject_init_code(content, package, package_conf["keep_roots"])
        Path(outfile).write_text(content, encoding="utf-8-sig")
        _LOG.info("patched __init__.py: %s", package)
        return outfile, copied

    def run(self) -> None:
        super().run()
        self._copy_bridge_package_inits()
        self._copy_bridge_package_data()
        self._copy_keep_modules()
        self._generate_version_files()

    def _copy_bridge_package_inits(self) -> None:
        """
        bridge 包需要把 vendor 子包的 __init__.py 同步到目标包树。

        否则像 ``xensesdk.retiles.chrome.context`` 这种导入在进入
        ``chrome.context`` 之前，会先尝试导入 ``xensesdk.retiles.chrome``。
        如果磁盘上没有 ``chrome/__init__.py``，Python 会把 ``chrome`` 当成
        需要直接加载的扩展模块，进而触发 ``PyInit_chrome`` 一类错误。
        """
        for package, package_conf in self.package_configs.items():
            if not package_conf["is_bridge"]:
                continue

            output_root = _package_output_dir(self.build_lib, package)
            source_root = package_conf["source_root"]

            for init_src in sorted(source_root.rglob("__init__.py")):
                rel_path = init_src.relative_to(source_root)
                if rel_path.as_posix() == "__init__.py":
                    # 根 __init__.py 使用 bridge 自己那份，不能被 vendor 覆盖。
                    continue

                init_dst = output_root / rel_path
                _copy_if_exists(init_src, init_dst)
                if init_dst.exists():
                    self._generated_outputs.append(str(init_dst))

            _LOG.info("copied bridge package __init__.py files: %s", package)

    def _copy_bridge_package_data(self) -> None:
        """把 bridge vendor 里的非 Python 资源复制到 bridge 包目录。"""
        for package, package_conf in self.package_configs.items():
            if not package_conf["is_bridge"]:
                continue

            output_root = _package_output_dir(self.build_lib, package)
            source_root = package_conf["source_root"]

            for src in sorted(source_root.rglob("*")):
                if not src.is_file():
                    continue
                if "__pycache__" in src.parts:
                    continue
                if src.suffix in {".py", ".pyx", ".c", ".pyc", ".pyo"}:
                    continue

                dst = output_root / src.relative_to(source_root)
                _copy_if_exists(src, dst)
                if dst.exists():
                    self._generated_outputs.append(str(dst))

            _LOG.info("copied bridge package data files: %s", package)

    def _copy_keep_modules(self) -> None:
        """把 keep 文件复制到目标包树，并补齐沿途子包 __init__.py。"""
        for package, package_conf in self.package_configs.items():
            output_root = _package_output_dir(self.build_lib, package)
            source_root = package_conf["source_root"]
            keep_paths = _resolve_keep_paths(source_root, package_conf["keep_modules"])

            for rel_path in sorted(keep_paths):
                src = source_root / rel_path
                dst = output_root / rel_path
                _copy_if_exists(src, dst)
                if dst.exists():
                    self._generated_outputs.append(str(dst))

                for parent in Path(rel_path).parents:
                    if str(parent) == ".":
                        continue
                    init_src = source_root / parent / "__init__.py"
                    init_dst = output_root / parent / "__init__.py"
                    _copy_if_exists(init_src, init_dst)
                    if init_dst.exists():
                        self._generated_outputs.append(str(init_dst))

            if package_conf["is_bridge"]:
                _LOG.info("copied keep files for bridge: %s", package)

    def _generate_version_files(self) -> None:
        """在每个顶层编译包根目录生成 _version.py。"""
        version = self.distribution.metadata.version
        top_packages = {
            package.split(".", 1)[0]
            for package in self.package_configs
            if "." not in package or package.split(".", 1)[0] == package
        }

        for package in top_packages:
            version_file = _package_output_dir(self.build_lib, package) / "_version.py"
            version_file.parent.mkdir(parents=True, exist_ok=True)
            version_file.write_text(f'__version__ = "{version}"\n', encoding="utf-8")
            self._generated_outputs.append(str(version_file))
            _LOG.info("generated version file: %s", version_file)

    def get_outputs(self, include_bytecode: bool = True) -> List[str]:
        """把动态生成的文件也纳入安装输出清单。"""
        outputs = list(super().get_outputs(include_bytecode=include_bytecode))
        for path in self._generated_outputs:
            if path not in outputs:
                outputs.append(path)
        return outputs


class _build_ext(original_build_ext):
    """自定义 build_ext：压缩编译器原始命令输出。"""

    def build_extensions(self) -> None:
        compiler = self.compiler
        original_spawn = compiler.spawn

        def quiet_spawn(cmd, **kwargs):
            message = _short_build_message(cmd)
            if message:
                _LOG.info(message)
            previous = distutils_log.set_threshold(distutils_log.WARN)
            try:
                return original_spawn(cmd, **kwargs)
            finally:
                _restore_log_threshold(previous)

        compiler.spawn = quiet_spawn
        try:
            super().build_extensions()
        finally:
            compiler.spawn = original_spawn


def build_cypack(setup_dict: Dict[str, Any], conf: Any = True):
    """
    把 cypack 的构建逻辑注入到 setuptools 的 setup() 参数中。

    Args:
        setup_dict: setup() 使用的 kwargs 字典。
        conf: False 表示禁用；dict 表示覆盖默认配置；True 表示用默认配置。
    """
    global _conf

    if not conf:
        return
    if os.environ.get("CYPACK", "True").lower() in {"0", "false"}:
        warnings.warn("Ignore cypack")
        return

    if isinstance(conf, dict):
        _conf = {**_conf, **conf}

    package_configs = _build_package_configs(setup_dict, _conf)

    if _conf["ext_modules"]:
        compiled = cythonize(
            _compile_packages(package_configs),
            compiler_directives={"language_level": 3},
            build_dir="build/cypack",
        )
        ext_modules = setup_dict.get("ext_modules", [])
        if ext_modules:
            ext_modules.extend(compiled)
        else:
            setup_dict["ext_modules"] = compiled

    cmdclass = setup_dict.get("cmdclass", {})
    cmdclass["build_py"] = _build_py
    cmdclass["build_ext"] = _build_ext
    setup_dict["cmdclass"] = cmdclass

    if "CFLAGS" not in os.environ:
        os.environ["CFLAGS"] = "-O3"

    if os.environ.get("CYPACK_DEBUG"):
        pprint(setup_dict)


def cypack(setup, attr, value):
    """setuptools setup keyword 入口。"""
    del attr
    build_cypack(vars(setup), value)
