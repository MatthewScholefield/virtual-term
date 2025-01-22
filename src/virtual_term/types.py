from dataclasses import dataclass


@dataclass
class CommandResult:
    output: bytes
    return_code: int


class VirtualTermError(Exception):
    pass


class TerminalDeadError(VirtualTermError):
    pass


class CommandTimeoutError(VirtualTermError):
    pass
