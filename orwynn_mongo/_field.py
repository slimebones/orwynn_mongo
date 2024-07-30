from pydantic import BaseModel


class DocField(BaseModel):
    """
    Represents Doc field specification.
    """
    name: str
    unique: bool = False
    linked_doc: str | None = None

class UniqueFieldErr(Exception):
    @staticmethod
    def code():
        return "orwynn_mongo::unique_field_err"

