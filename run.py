"""Start the development server with auto-reload on the provided port (default: 8000)."""

import uvicorn

from src.seriousdb.config import HOST, PORT

if __name__ == "__main__":
    uvicorn.run("seriousdb.main:app", host=HOST, port=PORT, reload=True)
