class Benchmark:
    def __init__(self, instructions: str, max_loops: int) -> None:
        self.instructions = instructions.strip()
        self.max_loops = max_loops
    
    def finalize(self, final_state, model) -> None:
        """Override in a subclass or monkey‑patch on an instance."""
        pass
