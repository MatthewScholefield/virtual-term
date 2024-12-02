from dataclasses import dataclass


@dataclass
class CommandResult:
    output: str
    return_code: int


class VirtualTermError(Exception):
    pass


class TerminalDeadError(VirtualTermError):
    pass


class CommandTimeoutError(VirtualTermError):
    pass
