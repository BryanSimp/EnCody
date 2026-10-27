import uvicorn

from . import config
from .app import create_app

if __name__ == "__main__":
    uvicorn.run(create_app(), host="0.0.0.0", port=config.PORT, log_level="warning", access_log=False)
