from typing import AsyncGenerator
import pytest
from virtual_term import (
    VirtualTerm,
    CommandResult,
    TerminalDeadError,
    CommandTimeoutError,
)


@pytest.fixture
async def term() -> AsyncGenerator[VirtualTerm, None]:
    term = await VirtualTerm.spawn()
    # await asyncio.sleep(3)
    yield term
    term.terminate()


@pytest.mark.asyncio
async def test_spawn_terminal(term: VirtualTerm) -> None:
    assert term.isalive()


@pytest.mark.asyncio
async def test_spawn_validate_last_command():
    import os
    os.environ['TEST_VALIDATE_LAST_COMMAND'] = '1'
    try:
        term = await VirtualTerm.spawn()
        term.terminate()
    finally:
        del os.environ['TEST_VALIDATE_LAST_COMMAND']


@pytest.mark.asyncio
async def test_run_command(term: VirtualTerm) -> None:
    result: CommandResult = await term.run_command('echo Hello, World!')
    assert 'Hello, World!' in result.output
    assert result.return_code == 0


@pytest.mark.asyncio
async def test_set_and_get_winsize(term: VirtualTerm) -> None:
    term.setwinsize(30, 100)
    rows: int
    cols: int
    rows, cols = term.getwinsize()
    assert rows == 30
    assert cols == 100


@pytest.mark.asyncio
async def test_terminate_terminal(term: VirtualTerm) -> None:
    term.terminate()
    assert not term.isalive()


@pytest.mark.asyncio
async def test_terminal_alive_status(term: VirtualTerm) -> None:
    assert term.isalive()
    term.terminate()
    assert not term.isalive()


@pytest.mark.asyncio
async def test_command_timeout(term: VirtualTerm) -> None:
    with pytest.raises(CommandTimeoutError):
        await term.run_command('sleep 5', update_timeout=0.5)
    term.ctrl_c()
    result = await term.wait_for_last_command()
    assert result.return_code == 130
    result: CommandResult = await term.run_command('echo $((11 + 22))')
    assert result.return_code == 0
    assert '33' in result.output


@pytest.mark.asyncio
async def test_terminal_dead_error(term: VirtualTerm) -> None:
    term.terminate()
    with pytest.raises(TerminalDeadError):
        await term.run_command('echo This should fail')
