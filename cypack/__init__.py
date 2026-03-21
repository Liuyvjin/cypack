from .init import init

__all__ = ["init"]

try:
    import setuptools
    from .build import build_cypack, cypack

    __all__ = ["init", "build_cypack", "cypack"]
except ImportError:
    pass
