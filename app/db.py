from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase

from .config import get_settings

_settings = get_settings()
_client = AsyncIOMotorClient(_settings.mongo_uri)
_db = _client[_settings.mongo_db]


def get_db() -> AsyncIOMotorDatabase:
    return _db


def get_collection(name: str):
    return _db[name]
