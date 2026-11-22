from typing import Annotated, Literal

from pydantic import Field, StringConstraints

from .schemas import Input, Note


class ClaimCallback(Input):
    claim_key: Annotated[str, StringConstraints(strip_whitespace=True, min_length=8, max_length=64)]
    appointment: int = Field(ge=0)


class ClaimReceipt(Input):
    channel: Literal['phone', 'in_person']
    outcome: Literal['no_answer', 'requested_callback', 'reached']
    note: Note
