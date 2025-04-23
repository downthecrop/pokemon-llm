# pokemon-llm

- Currently only supports Gen1 but Gen2 & Gen3 support are planned
- Requires mGBA with scripting autolaunch, [dev builds support this.](https://mgba.io/downloads.html#development-downloads)

## DUMP MAP IMAGE

python dump.py red.gb 56 -o mart.png -d --start 7,7 --end 0,2

| Normal | Debug | Path | Minimal |
|---------|---------|---------|---------|
| ![Alt1](images/normal_mart.png) | ![Alt2](images/debug_mart.png) | ![Alt3](images/path_debug_mart.png) | ![Alt4](images/minimal_mart.png) |

## RUN

python run.py --windowed --auto 

> Configure mGBA not to use GB Player features.

![Alt1](images/no_gbp.png)