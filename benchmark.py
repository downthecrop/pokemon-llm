import importlib.util
import pathlib

class Benchmark:
    def __init__(self, instructions: str, max_loops: int) -> None:
        self.instructions = instructions.strip()
        self.max_loops = max_loops
    
    def finalize(self, final_state, model) -> None:
        """Override in a subclass or monkey-patch on an instance."""
        pass

    def validation(self, state) -> bool:
        return False

def load(path: str):
    """
    Load an arbitrary benchmark file and return the object created by its init() function.

    Expected contract:
        - The file defines an `init()` function that returns a subclass of Benchmark.
    """
    file = pathlib.Path(path).expanduser().resolve()
    if not file.exists():
        raise FileNotFoundError(file)

    spec = importlib.util.spec_from_file_location(file.stem, str(file))
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot import from {file}")

    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    if not hasattr(module, "init"):
        raise AttributeError(f"{file} does not define an init() function")

    bench = module.init()
    return bench