"""Shared strict model base for all public LinguaWiki contracts."""

from pydantic import BaseModel, ConfigDict


class ContractModel(BaseModel):
    """Immutable, closed-world base for versioned wire models."""

    model_config = ConfigDict(extra="forbid", frozen=True, use_enum_values=True)
