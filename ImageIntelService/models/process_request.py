from pydantic import BaseModel, field_validator
import base64


class ProcessRequest(BaseModel):
    image_base64: str

    @field_validator("image_base64")
    @classmethod
    def must_be_nonempty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("image_base64 must not be empty")
        return v
