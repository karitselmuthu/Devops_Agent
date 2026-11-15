"""Development entry point: uv run python main.py."""

import uvicorn


if __name__ == "__main__":
    uvicorn.run("agents_platform.api:app", host="127.0.0.1", port=8000, reload=True)

# See PyCharm help at https://www.jetbrains.com/help/pycharm/
