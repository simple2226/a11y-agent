"""Mangum adapter so the FastAPI app runs on Lambda behind API Gateway."""

from mangum import Mangum

from api.main import app

lambda_handler = Mangum(app, lifespan="off")
