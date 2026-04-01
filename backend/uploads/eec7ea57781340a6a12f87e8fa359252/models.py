from typing import Optional

from pydantic import BaseModel, field_validator


class StoreRecord(BaseModel):
    link: Optional[str] = None
    address: Optional[str] = None
    state: Optional[str] = None
    phone: Optional[str] = None
    time: Optional[str] = None

    @field_validator("link", "address", "state", "phone", "time", mode="before")
    def strip_blank(cls, value):
        if value is None:
            return None
        value = str(value).strip()
        return value or None

    def as_dict(self) -> dict:
        return self.model_dump()
