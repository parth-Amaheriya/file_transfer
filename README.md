# FastAPI Transfer Service

This service exposes session-based APIs for transferring files, sharing structured logs, and coordinating devices over the network.

## Features
- Session creation plus device registration surfaced through a FastAPI-rendered control room
- Chunk-friendly file uploads saved to disk (or S3/minio in production)
- MongoDB persistence for sessions and immutable log trails
- WebSocket channel that streams log updates to connected clients (API consumers can still subscribe)

## Getting Started
1. Create a Python 3.12 virtual environment.
2. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
3. Copy `.env.example` to `.env` and set your values.
4. Run the server:
   ```bash
   uvicorn app.main:app --reload
   ```

Then browse to `http://localhost:8000` for the Python-rendered dashboard.

## Environment Variables
| Name | Description | Default |
| --- | --- | --- |
| `MONGO_URI` | MongoDB connection string | `mongodb://localhost:27017` |
| `MONGO_DB` | Database name | `transfer_hub` |
| `UPLOADS_PATH` | Directory used to store uploaded binaries | `backend/uploads` |

## Testing
```bash
pytest
```
_(Tests not included in skeleton yet.)_
