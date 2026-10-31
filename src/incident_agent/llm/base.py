"""The pair of model tiers the pattern needs."""

from collections.abc import Mapping
from dataclasses import dataclass

from strands.models import Model


@dataclass
class ModelSet:
    """A stronger model for the orchestrator and cheaper models for the workers."""

    orchestrator: Model
    workers: Mapping[str, Model]
    orchestrator_model_id: str
    worker_model_id: str
