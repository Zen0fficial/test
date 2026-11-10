"""Mongo-free Qlib task coordination with immutable Parquet execution history."""

from .manager import LeaseLost, TaskFailure, TaskManager
from .trainer import DelayTrainerRM, TrainerRM, run_task

ParquetTaskManager = TaskManager
ParquetTrainerRM = TrainerRM
ParquetDelayTrainerRM = DelayTrainerRM
SQLiteTaskStore = TaskManager

__all__ = ["DelayTrainerRM", "LeaseLost", "ParquetDelayTrainerRM", "ParquetTaskManager", "ParquetTrainerRM",
           "SQLiteTaskStore", "TaskFailure", "TaskManager", "TrainerRM", "run_task"]
