def parse_max_loops_fn(value):
    try:
        value = int(value)
        if value <= 0:
            return [None, False, "--max_loops value must be a positive integer."]

        return [value, True, f"Command line argument: --max_loops set to {value}."]

    except ValueError:
        return [None, False, f"--max_loops expects an integer value, got: {value}"]
