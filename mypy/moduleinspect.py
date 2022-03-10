"""Basic introspection of modules."""

from typing import List, Optional, Union
from types import ModuleType
from multiprocessing import Process, Queue
import importlib
import inspect
import os
import pkgutil
import queue
import sys


class ModuleProperties:
    def __init__(self,
                 name: str,
                 file: Optional[str],
                 path: Optional[List[str]],
                 all: Optional[List[str]],
                 is_c_module: bool,
                 subpackages: List[str]) -> None:
        self.name = name  # __name__ attribute
        self.file = file  # __file__ attribute
        self.path = path  # __path__ attribute
        self.all = all  # __all__ attribute
        self.is_c_module = is_c_module
        self.subpackages = subpackages


def is_c_module(module: ModuleType) -> bool:
    if module.__dict__.get('__file__') is None:
        # Could be a namespace package. These must be handled through
        # introspection, since there is no source file.
        return True
    return os.path.splitext(module.__dict__['__file__'])[-1] in ['.so', '.pyd']


class InspectError(Exception):
    pass


def get_package_properties(package_id: str) -> ModuleProperties:
    """Use runtime introspection to get information about a module/package."""
    try:
        package = importlib.import_module(package_id)
    except BaseException as e:
        raise InspectError(str(e)) from e
    name = getattr(package, "__name__", package_id)
    file = getattr(package, "__file__", None)
    path: Optional[List[str]] = getattr(package, "__path__", None)
    if not isinstance(path, list):
        path = None
    pkg_all = getattr(package, '__all__', None)
    if pkg_all is not None:
        try:
            pkg_all = list(pkg_all)
        except Exception:
            pkg_all = None
    is_c = is_c_module(package)

    if path is None:
        # Object has no path; this means it's either a module inside a package
        # (and thus no sub-packages), or it could be a C extension package.
        if is_c:
            # This is a C extension module, now get the list of all sub-packages
            # using the inspect module
            subpackages = [
                f'{package.__name__}.{name}'
                for name, val in inspect.getmembers(package)
                if inspect.ismodule(val)
                and val.__name__ == f'{package.__name__}.{name}'
            ]

        else:
            # It's a module inside a package.  There's nothing else to walk/yield.
            subpackages = []
    else:
        all_packages = pkgutil.walk_packages(path, prefix=package.__name__ + ".",
                                             onerror=lambda r: None)
        subpackages = [qualified_name for importer, qualified_name, ispkg in all_packages]
    return ModuleProperties(name=name,
                            file=file,
                            path=path,
                            all=pkg_all,
                            is_c_module=is_c,
                            subpackages=subpackages)


def worker(tasks: 'Queue[str]',
           results: 'Queue[Union[str, ModuleProperties]]',
           sys_path: List[str]) -> None:
    """The main loop of a worker introspection process."""
    sys.path = sys_path
    while True:
        mod = tasks.get()
        try:
            prop = get_package_properties(mod)
        except InspectError as e:
            results.put(str(e))
            continue
        results.put(prop)


class ModuleInspect:
    """Perform runtime introspection of modules in a separate process.

    Reuse the process for multiple modules for efficiency. However, if there is an
    error, retry using a fresh process to avoid cross-contamination of state between
    modules.

    We use a separate process to isolate us from many side effects. For example, the
    import of a module may kill the current process, and we want to recover from that.

    Always use in a with statement for proper clean-up:

      with ModuleInspect() as m:
          p = m.get_package_properties('urllib.parse')
    """

    def __init__(self) -> None:
        self._start()

    def _start(self) -> None:
        self.tasks: Queue[str] = Queue()
        self.results: Queue[Union[ModuleProperties, str]] = Queue()
        self.proc = Process(target=worker, args=(self.tasks, self.results, sys.path))
        self.proc.start()
        self.counter = 0  # Number of successful roundtrips

    def close(self) -> None:
        """Free any resources used."""
        self.proc.terminate()

    def get_package_properties(self, package_id: str) -> ModuleProperties:
        """Return some properties of a module/package using runtime introspection.

        Raise InspectError if the target couldn't be imported.
        """
        self.tasks.put(package_id)
        res = self._get_from_queue()
        if res is None:
            # The process died; recover and report error.
            self._start()
            raise InspectError('Process died when importing %r' % package_id)
        if isinstance(res, str):
            # Error importing module
            if self.counter > 0:
                # Also try with a fresh process. Maybe one of the previous imports has
                # corrupted some global state.
                self.close()
                self._start()
                return self.get_package_properties(package_id)
            raise InspectError(res)
        self.counter += 1
        return res

    def _get_from_queue(self) -> Union[ModuleProperties, str, None]:
        """Get value from the queue.

        Return the value read from the queue, or None if the process unexpectedly died.
        """
        max_iter = 100
        n = 0
        while True:
            if n == max_iter:
                raise RuntimeError('Timeout waiting for subprocess')
            try:
                return self.results.get(timeout=0.05)
            except queue.Empty:
                if not self.proc.is_alive():
                    return None
            n += 1

    def __enter__(self) -> 'ModuleInspect':
        return self

    def __exit__(self, *args: object) -> None:
        self.close()
