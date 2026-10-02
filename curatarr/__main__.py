import os

from . import create_app

if __name__ == "__main__":
    create_app().run(
        host=os.getenv("CURATARR_HOST", "0.0.0.0"),
        port=int(os.getenv("CURATARR_PORT", "8787")),
    )
