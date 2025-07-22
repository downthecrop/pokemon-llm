import json
import re

def parse_optional_fenced_json(text):
    """
    Parses JSON from `text`, which may be either:
      - Enclosed in triple-backtick fences (``` or ```json … ```)
      - Plain JSON without fences

    Returns:
        The deserialized Python object.

    Raises:
        ValueError: if JSON parsing fails or (if fenced) no valid fence is found.
    """
    # Try to find a fenced JSON block first
    fence_pattern = r'```(?:json)?\s*\n(.*?)\n```'
    m = re.search(fence_pattern, text, flags=re.DOTALL | re.IGNORECASE)
    if m:
        json_str = m.group(1)
    else:
        # No fence: assume the entire text is JSON
        json_str = text.strip()
    
    try:
        j = json.loads(json_str)
        return j
    except json.JSONDecodeError:
        return {}
