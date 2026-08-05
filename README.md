# Data Lineage Investigator

Data Lineage Investigator is an AI-powered agent system that traces the origin, transformation, and flow of data across a data pipeline or database, helping engineers and analysts answer questions like "where did this value come from?" and "what downstream tables/reports does this column affect?" by combining a LangGraph-orchestrated multi-agent workflow, retrieval over schema/documentation stores, and direct querying of a Postgres-backed database through a FastAPI service. (More details to be added.)

## Setup

1. Create and activate a virtual environment (Python 3.11+):

   ```bash
   python -m venv .venv
   .venv\Scripts\activate  # Windows
   source .venv/bin/activate  # macOS/Linux
   ```

2. Install dependencies:

   ```bash
   pip install -r requirements.txt
   ```

3. Copy `.env.example` to `.env` and fill in your credentials:

   ```bash
   cp .env.example .env
   ```

4. Run the API:

   ```bash
   uvicorn app.api.main:app --reload
   ```

5. Check the health endpoint at [http://127.0.0.1:8000/health](http://127.0.0.1:8000/health).

## Project Structure

```
/app
  /agents        # Agent definitions
  /graph         # LangGraph workflow graphs
  /retrieval     # Retrieval / vector store logic
  /sandbox_data  # Sample/sandbox data for local testing
  /db            # Database models and access
  /api           # FastAPI application
/tests           # Test suite
```
