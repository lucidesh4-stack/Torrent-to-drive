import os
import uvicorn

if __name__ == "__main__":
    port = int(os.getenv("PORT", "7860"))
    host = os.getenv("HOST", "0.0.0.0")
    print(f"Launching CloudFlow on http://{host}:{port}")
    uvicorn.run("streamly.app:create_app", host=host, port=port, factory=True, workers=1)
