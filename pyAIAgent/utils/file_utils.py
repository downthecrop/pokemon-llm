import os
import sys
import shutil
from tqdm import tqdm

def find_mgba():
    # os.name == "nt" for window
    exe_name = "mgba.exe" if os.name == "nt" else "mgba"
    path = shutil.which(exe_name)
    if path:
        print(f"found in {path}")
        return path

    # fallback for search common install locations
    search_paths = []
    
    if sys.platform.startswith("win"):
        search_paths = [
            "C:\\Program Files\\mGBA",
            "C:\\Program Files (x86)\\mGBA",
            os.path.expanduser("~\\Downloads\\mGBA")
        ]
    elif sys.platform == "darwin":  # macOS
        search_paths = [
            "/Applications/mGBA.app/Contents/MacOS",
            "/usr/local/bin",
            os.path.expanduser("~/Applications"),
        ]
    elif sys.platform.startswith("linux"):
        search_paths = [
            "/usr/bin",
            "/usr/local/bin",
            os.path.expanduser("~/Downloads"),
            os.path.expanduser("~/.local/bin"),
        ]

    for folder in search_paths:
        full_path = os.path.join(folder, exe_name)
        if os.path.exists(full_path):
            return full_path
    
    print_dot_interval = 2000 # print dot every 2000 dirs
    print("Finding mgba file path...", end="", flush=True)
    
    # Count total directories first (for accurate progress)
    
    dirs = []
    count = 0

    for root, _, _ in os.walk(os.path.expanduser("~")):
        dirs.append(root)
        count += 1
        if count % print_dot_interval == 0:
            print(".", end="", flush=True)
        
    print()
    

    for root in tqdm(dirs, desc="Scanning directories"):
        try:
            if exe_name in os.listdir(root):
                return os.path.join(root, exe_name)
        except (PermissionError, FileNotFoundError):
            continue
    print("MGBA not found!")
    return None
