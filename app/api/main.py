from fastapi import FastAPI

app = FastAPI(title="Data Lineage Investigator")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
