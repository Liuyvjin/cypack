# cypack

Compile Python packages into single `__compile__` extensions with support for `src/` layouts and one-layer vendor bridge packages.

## 配置说明

每个需要参与编译的包，都可以在包目录下放一个 `__compile__.py`。

支持的常用配置：

```python
exclude = ["examples/demo.py"]
keep_modules = ["config.py", "examples/*.py"]
vendor_path = "../_vendor/sub_proj/sub_module"
```

### 路径是相对于谁写

`exclude` / `keep_modules` 的路径，**永远相对于当前编译包的源码根目录**，不是相对于项目根目录。

- 普通包：
  - 源码根目录 = 当前包目录
- bridge 包：
  - 源码根目录 = `vendor_path` 指向的真实源码目录

例如项目结构：

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
keep_modules = ["src/main_module/config.py"]      # 错
exclude = ["main_module/examples/a.py"]           # 错
```

### keep_modules 支持 glob

`keep_modules` 既支持精确路径，也支持 glob，例如：

```python
keep_modules = [
    "config.py",
    "examples/*.py",
]
```

含义：

- `config.py` 保留为原始 `.py`
- `examples/*.py` 下命中的文件保留为原始 `.py`
- 这些模块不会被编译进 `__compile__`
- 运行时会绕过 import hook，继续按普通 Python 模块导入

### exclude 的语义

`exclude` 表示：

- 不编译
- 也不进入最终包

例如：

```python
exclude = ["experimental/ti_kernel.py"]
```

### keep_modules 的语义

`keep_modules` 表示：

- 不编译
- 但保留到最终包中
- 运行时按普通 `.py` 文件导入

例如希望下面命令在编译后仍可运行：

```bash
python -m retiles.examples.example_widget_context
```

则可写：

```python
keep_modules = ["examples/*.py"]
```
