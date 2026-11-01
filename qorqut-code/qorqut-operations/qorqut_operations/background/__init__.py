"""Registered-program scheduling, authority and serial job execution.

The host registers concrete programs (models, providers, repository status, ...) in a ``ProgramRegistry`` and
freezes it before serving; everything here is the generic machinery that runs them under their worker principals."""

from .authority import BackgroundAuthority
from .programs import Program, ProgramPending, ProgramRegistry, ProgramResult
from .routines import RoutineScheduler
from .schedule import RoutineError, matches, next_slot, parse_cron, prev_slot
from .service import BackgroundService, JobControl, JobObserver

__all__ = ["BackgroundAuthority", "BackgroundService", "JobControl", "JobObserver", "Program", "ProgramPending",
           "ProgramRegistry", "ProgramResult", "RoutineError", "RoutineScheduler", "matches", "next_slot",
           "parse_cron", "prev_slot"]
