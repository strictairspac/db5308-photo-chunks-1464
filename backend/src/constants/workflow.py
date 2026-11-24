from enum import StrEnum


class Role(StrEnum):
    OPERATOR = 'operator'
    REVIEWER = 'reviewer'
    VIEWER = 'viewer'


class Status(StrEnum):
    QUEUED = 'queued'
    RUNNING = 'running'
    COMPLETE = 'complete'
    FAILED = 'failed'
    VOIDED = 'voided'


class RequestStatus(StrEnum):
    ACTIVE = 'active'
    WITHDRAWN = 'withdrawn'
