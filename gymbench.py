# -- gymbench.py --
import logging
from benchmark import Benchmark

class GymBenchmark(Benchmark):
    def finalize(self, final_state, model) -> None:
        logging.info(
            "Run Final State: %s\n\nBadges: %s\nParty: %s",
            model,
            final_state.get("badges"),
            final_state.get("party"),
        )


bench_instructions = """
- YOUR GOAL IS TO ENTER THE GYM AND DEFEAT BROCK.
- DO NOT LEAVE THE GYM ONCE YOU ENTER. YOUR PARTY IS STRONG ENOUGH TO COMPLETE IT WITHOUT HEALING.
- DO NOT LEAVE THE GYM TO HEAL YOUR POKEMON.
"""

def init():
    return GymBenchmark(bench_instructions, 5)