from fastapi import Body, FastAPI
from typing import Any

from app.pipeline.runner101 import run_colab101

app = FastAPI(title="cash-ai-02")


@app.get("/health")
def health():
    return {"ok": True}


@app.post("/v1/pipeline")
def pipeline(payload: Any = Body(...)):
    return run_colab101(payload)
