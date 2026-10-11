# Employee Attendance & Analytics API

A FastAPI service backed by MongoDB for employee records, attendance punch-in/punch-out, attendance corrections, and analytics.

## Requirements

- Python 3.10 or newer
- MongoDB 6.0 or newer (the grader uses MongoDB 7.0)
- Git

MongoDB Atlas M0 is supported.

## Setup

Create and activate a virtual environment.




### Windows PowerShell

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
```

Install dependencies:

```bash
pip install -r requirements.txt
```

Create your local environment file from the example:

```bash
cp .env.example .env
```

On Windows PowerShell, use:

```powershell
Copy-Item .env.example .env
```

Set `MONGO_URI` and `MONGO_DB` in `.env`. Do not commit `.env` or database credentials.

For a local MongoDB instance, you can use Docker:

```bash
docker run -p 27017:27017 mongo:7
```

Load the provided sample data:

```bash
python sample_seed.py
```

## Run the API

From the project root, start the service with the required command:

```bash
python -m uvicorn app.main:app --port 8000
```

For development with automatic reload:

```bash
python -m uvicorn app.main:app --port 8000 --reload
```

- Health endpoint: `http://127.0.0.1:8000/health`
- Interactive Swagger documentation: `http://127.0.0.1:8000/docs`
- OpenAPI schema: `http://127.0.0.1:8000/openapi.json`

## API capabilities

The API provides employee creation and listing, attendance punch-in and punch-out, attendance listing and regularization, monthly employee analytics, department summaries, late-arrival leaderboard, department trends, and database query-plan explanations.

See the interactive Swagger page and `openapi.yaml` for endpoint parameters, response formats, and business rules.

## Submission contents

The public Git repository should include:

- `app/main.py` — application implementation
- `requirements.txt` — Python dependencies
- `REVIEW.md` — starter-code defect review
- `DECISIONS.md` — implementation decisions
- `README.md` — setup and run instructions

Keep `.env`, credentials, and other secrets out of the repository. The candidate-kit instructions specify that no Dockerfile should be submitted.
