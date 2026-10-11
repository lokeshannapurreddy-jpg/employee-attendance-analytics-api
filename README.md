# Employee Attendance & Analytics API

A FastAPI backend for employee attendance tracking, attendance regularization, and workforce analytics, backed by MongoDB.

## Live Application

- **Swagger UI:** https://employee-attendance-analytics-api.onrender.com/docs
- **Health endpoint:** https://employee-attendance-analytics-api.onrender.com/health
- **GitHub repository:** https://github.com/lokeshannapurreddy-jpg/employee-attendance-analytics-api

The base URL may return {"detail":"Not Found"} because the API does not define a homepage route. Use /docs to explore the API.

## Features

- Employee listing and creation
- Attendance punch-in and punch-out
- Attendance record listing and regularization with audit history
- Monthly employee attendance analytics
- Department attendance summaries
- Late-arrival leaderboard with competition ranking
- Daily department trends and seven-day moving averages
- MongoDB query explainability for supported endpoints

## Technology Stack

- Python
- FastAPI
- MongoDB Atlas
- PyMongo
- Pydantic

## Configuration

Set these environment variables:

- MONGO_URI: MongoDB connection URI
- MONGO_DB: Database name, such as attendance_db

Keep database credentials in environment variables or a local .env file. Never commit secrets to GitHub.

## Run Locally

From the project directory, install the dependencies:

    python -m pip install -r requirements.txt

Configure MONGO_URI and MONGO_DB, then run:

    uvicorn app.main:app --reload --port 8000

Open the local Swagger UI at:

    http://127.0.0.1:8000/docs

## Project Documentation

- REVIEW.md — findings, observed verification results, and remaining tests.
- DECISIONS.md — implementation decisions and their rationale.

## Notes

The public deployment uses a hosted service and MongoDB Atlas. Free hosting may introduce a delay when the application starts after being idle.

Successful responses on a small sample dataset do not establish performance at large scale. Query plans and business-rule boundary cases should be validated against the assignment requirements.
