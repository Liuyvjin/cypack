# cypack

把 Python 包编译成单个 `__compile__` 扩展模块，支持：

- 普通包编译
- `src/` 布局
- 一层 vendor bridge
- `exclude` / `keep_modules`
- 自动生成 `_version.py`

---

## 1. 基本用法

先安装：

```bash
pip install cypack
```

在 `setup.py` 里启用：

```python
from setuptools import setup, find_packages

setup(
    name="demo",
    version="0.1.0",
    packages=find_packages(),
    setup_requires=["cypack[build]"],
    cypack=True,
)
```

如果是 `src/` 布局：

```python
from setuptools import setup, find_namespace_packages

setup(
    name="demo",
    version="0.1.0",
    packages=find_namespace_packages("src"),
    package_dir={"": "src"},
    setup_requires=["cypack[build]"],
    cypack=True,
)
```

然后在需要编译的包目录下放一个 `__compile__.py` 即可。

---

## 2. 普通包编译

例如：

```text
src/
  main_module/
    __init__.py
    __compile__.py
    core.py
    utils.py
```

其中：

```python
# src/main_module/__compile__.py
exclude = []
keep_modules = []
```

构建后通常会得到：

```text
main_module/
  __init__.py
  __compile__.so   # Windows 下是 .pyd
  _version.py
```

其中：

- `__init__.py` 默认保留
- 其余 `.py` 会被编译进 `__compile__`
- 顶层编译包会自动生成 `_version.py`

---

## 3. __compile__.py 支持哪些字段

常用配置：

```python
exclude = ["examples/demo.py"]
keep_modules = ["config.py", "examples/*.py"]
vendor_path = "../_vendor/sub_proj/sub_module"
```

含义：

- `exclude`
  - 不编译
  - 也不进入最终包
- `keep_modules`
  - 不编译
  - 但保留为原始 `.py`
  - 运行时按普通 Python 模块导入
- `vendor_path`
  - 仅 bridge 包使用
  - 表示真实源码位于 vendor 目录

---

## 4. 路径相对于谁写

`exclude` / `keep_modules` 的路径，**永远相对于当前编译包的源码根目录**。

- 普通包：
  - 源码根目录 = 当前包目录
- bridge 包：
  - 源码根目录 = `vendor_path` 指向的真实源码目录

例如：

```text
src/
  main_module/
    __compile__.py
    config.py
    examples/
      a.py
```

那么应写：

```python
keep_modules = ["config.py", "examples/*.py"]
exclude = ["examples/a.py"]
```

而不是：

```python
keep_modules = ["src/main_module/config.py"]   # 错
exclude = ["main_module/examples/a.py"]        # 错
```

---

## 5. keep_modules 支持 glob

例如：

```python
keep_modules = [
    "config.py",
    "examples/*.py",
]
```

效果：

- `config.py` 保留
- `examples/*.py` 命中的文件保留
- 它们不会被编译进 `__compile__`
- 运行时会绕过 cypack 的 import hook

这类配置适合：

- 示例脚本
- 命令行入口脚本
- 需要 `python -m package.module` 直接运行的模块

例如：

```python
keep_modules = ["examples/*.py"]
```

这样编译后仍可运行：

```bash
python -m retiles.examples.example_widget_context
```

---

## 6. bridge 包是什么

bridge 包用于把 `_vendor` 里的子工程映射到主包命名空间下。

例如希望开发态 / 编译态都能这样导入：

```python
import main_module.sub_module
from main_module.sub_module.core import sub
```

项目结构可以是：

```text
src/
  main_module/
    _vendor/
      sub_proj/
        sub_module/
          __init__.py
          core.py
    sub_module/
      __init__.py
      __compile__.py
```

其中 bridge 配置：

```python
# src/main_module/sub_module/__compile__.py
vendor_path = "../_vendor/sub_proj/sub_module"
keep_modules = []
exclude = []
```

---

## 7. CYPACK-DEV-BEGIN / CYPACK-DEV-END 是什么

bridge 包的 `__init__.py` 里，通常会写一段**仅开发态使用**的代理代码，例如：

```python
# CYPACK-DEV-BEGIN
from pathlib import Path as _Path

_VENDOR_ROOT = _Path(__file__).resolve().parent.parent / "_vendor" / "sub_proj" / "sub_module"
__path__ = [str(_VENDOR_ROOT)] + __path__
exec((_VENDOR_ROOT / "__init__.py").read_text(encoding="utf-8"), globals(), globals())
# CYPACK-DEV-END
```

这两个标记的作用是：

- `# CYPACK-DEV-BEGIN`
- `# CYPACK-DEV-END`

告诉 cypack：

> 这中间的代码只给源码开发阶段使用，构建最终包时需要自动剔除。

也就是说：

- 开发态：bridge 可以直接代理到 `_vendor` 真实包，便于导入和补全
- 编译态：这段代码会被删掉，改由 `cypack.init()` + `__compile__` 接管

如果你写 bridge，建议始终把开发态代理逻辑包在这两个标记中间。

---

## 8. _version.py 是什么

cypack 会对**顶层编译包**自动生成一个 `_version.py`，内容类似：

```python
__version__ = "0.1.0"
```

例如：

```text
main_module/
  __init__.py
  __compile__.pyd
  _version.py
```

使用方式：

```python
from main_module._version import __version__
```

说明：

- 版本号来自 `setup.py` / `setup.cfg` / setuptools metadata
- 只对顶层编译包生成
- 子 bridge 包通常不会单独生成 `_version.py`

---

## 9. bridge 构建后的资源和 __init__.py 放哪

bridge 会把 vendor 包“吸收”到 bridge 自己目录下。

例如 vendor 里有：

```text
_vendor/retiles/retiles/assets/...
_vendor/retiles/retiles/chrome/__init__.py
_vendor/retiles/retiles/core/__init__.py
```

最终应映射到：

```text
xensesdk/retiles/assets/...
xensesdk/retiles/chrome/__init__.py
xensesdk/retiles/core/__init__.py
```

而不是继续保留在：

```text
xensesdk/_vendor/...
```

也就是说：

- vendor 的包结构会映射到 bridge 包目录
- bridge 构建时会忽略 vendor 内部自己的 `__compile__.py`
- `_vendor` 不应作为最终运行时导入路径

---

## 10. 子编译包为什么会出现在 keep roots 里

如果：

- `a` 是编译包
- `a.b` 也是编译包

那么 `a.__init__.py` 注入的：

```python
import cypack; cypack.init(__name__, set([...]))
```

里会自动把 `b` 放进去。

目的不是“保留源码”，而是：

> 让父包 `a` 在导入 `a.b.*` 时不要抢先截获，而是把导入继续下沉给 `a.b` 自己的 finder 处理。

这是为了避免父子两个 `__compile__` 同时接管同一批模块。

---

## 11. package_data 应该写到哪里

如果你用了 bridge，例如：

- `xensesdk.ezgl`
- `xensesdk.retiles`

那么资源的 `package_data` 也应写到 bridge 包名上，而不是 `_vendor` 包名上。

正确示例：

```python
package_data = {
    "xensesdk.ezgl": ["resources/**/*.*", "resources/*.*"],
    "xensesdk.retiles": ["assets/**/*.*", "assets/*.*"],
}
```

不建议再写：

```python
package_data = {
    "xensesdk._vendor.retiles.retiles": [...],   # 不建议
    "xensesdk._vendor.ezgl.ezgl.resources": [...],
}
```

因为 bridge 的最终目标是把资源放进：

- `xensesdk/ezgl/...`
- `xensesdk/retiles/...`

而不是 `_vendor/...`

---

## 12. 已知边界

当前实现的边界：

- 支持一层 vendor bridge
- bridge 只认 bridge 自己目录下的 `__compile__.py`
- vendor 内部自带的 `__compile__.py` 会被忽略
- 不保证 vendor 工程内部再次嵌套 vendor/bridge 的自动语义继承

---

## 13. 适合放在 keep_modules 的典型内容

常见建议：

```python
keep_modules = [
    "__main__.py",
    "examples/*.py",
    "config.py",
]
```

适合放 keep 的一般是：

- 直接执行的脚本
- 入口模块
- 编译后仍希望保留源码形态的配置模块

---

## 14. 调试

可用环境变量：

```bash
CYPACK_DEBUG=1
CYPACK_DEBUG_IMPORT=1
```

其中：

- `CYPACK_DEBUG=1`：输出构建阶段更多日志
- `CYPACK_DEBUG_IMPORT=1`：输出运行时 import hook 调试信息
