import asyncio
import os
from os.path import basename
import subprocess
from contextlib import suppress
from typing import AsyncGenerator, List, Tuple, Optional
import uuid
import shlex
import time
from pathlib import Path

from asyncinotify import Inotify, Mask

from .types import (
    CommandResult,
    VirtualTermError,
    TerminalDeadError,
    CommandTimeoutError,
)


class VirtualTerm:
    screen_prefix = 'screen_pty_term_'

    def __init__(self, id: str):
        self.screen_name = self.screen_prefix + id
        from tempfile import gettempdir

        self.log_file = Path(gettempdir()) / f'{self.screen_name}.log'
        self.size_file = Path(gettempdir()) / f'{self.screen_name}.size.txt'
        self.command_outputs_file = (
            Path(gettempdir()) / f'{self.screen_name}.outputs.txt'
        )
        self.log_file.touch()
        self.command_outputs_file.touch()
        self._fd = self.log_file.open('rb')
        self._commands_fd = self.command_outputs_file.open('rb')
        self.id = id
        self.log_file_offset = 0

    @classmethod
    async def spawn(
        cls,
        cwd: Path | None = None,
        dimensions=(24, 80),
    ):
        pty_process = cls(id=uuid.uuid4().hex)
        shell_command = shlex.join([os.environ.get('SHELL', '/bin/bash')])
        cd_prefix = f'cd {shlex.quote(str(cwd))}; ' if cwd else ''

        # Include an evaluated command that appends the last status code to a file we watch for changes
        prompt_var = {'bash': 'PS1', 'zsh': 'PROMPT'}[basename(shell_command)]
        command_outputs_file = str(pty_process.command_outputs_file)
        prompt_customization = f'{prompt_var}="\\\\$(echo \\\\$? >> {command_outputs_file})\\${prompt_var}"'

        prefix_indicator = f'prefix:{pty_process.id}'
        initial_command = (
            f'{cd_prefix}{prompt_customization}; echo "printed:"{prefix_indicator}'
        )

        pty_process._run_screen(
            f'-L -Logfile {pty_process.log_file.as_posix()} -dm {shell_command}'
        )
        pty_process._run_screen('-X logfile flush 0')
        pty_process.write(initial_command + '\n')
        pty_process.setwinsize(*dimensions)

        # Wait for us to see initial_command in the log file
        async for _ in pty_process.read_command_result_stream(1):
            pass
        prefix = pty_process._fd.read(10240)
        # Find last occurrence of prefix_indicator in the log file
        index = prefix.rfind(('printed:' + prefix_indicator).encode())
        if index == -1:
            raise VirtualTermError(f'Prefix indicator not found in log file: {prefix}')
        index += len(prefix_indicator)
        while index < len(prefix) and prefix[index] in b'\r\n':
            index += 1
        pty_process._fd.seek(index, os.SEEK_SET)
        pty_process._commands_fd.seek(0, os.SEEK_END)
        if os.environ.get('RUNNING_TESTS'):
            try:
                result = await pty_process.wait_for_last_command(global_timeout=0.2)
            except CommandTimeoutError:
                pass
            else:
                raise VirtualTermError(
                    f'Unexpected output after startup ({result.return_code}): {result.output}'
                )

        return pty_process

    async def run_command(
        self,
        command: str,
        update_timeout: Optional[float] = None,
        global_timeout: Optional[float] = None,
    ) -> CommandResult:
        """
        Run a command in the terminal session, waiting for the command to finish and returning the output.
        Note that the output will likely contain the command itself depending on the shell.

        Args:
            command: The command to run.
            update_timeout: The maximum time to wait for new output before raising a PtyTimeoutError.
            global_timeout: The maximum total time to wait for new output before raising a PtyTimeoutError.
        """
        # Clear the buffer
        self.read_new_output()
        self.read_new_command_results()

        self.write(command + '\r')
        return await self.wait_for_last_command(update_timeout, global_timeout)

    async def read_output_stream(
        self, size: Optional[int] = None
    ) -> AsyncGenerator[bytes, None]:
        """
        A generator that yields the output of the terminal session chunk by chunk.
        This should never be called concurrently since it reads from the same file descriptor.
        """
        async for _ in _watch_for_file_updates(self.log_file):
            data = self.read_new_output(size)
            if data:
                yield data

    async def wait_for_last_command(
        self,
        update_timeout: Optional[float] = None,
        global_timeout: Optional[float] = None,
    ) -> CommandResult:
        async for return_code in self.read_command_result_stream(
            1, update_timeout, global_timeout
        ):
            output = self.read_new_output().decode()
            return CommandResult(output, return_code)
        raise RuntimeError(
            'read_command_result_stream should never return without yielding a value'
        )

    async def read_command_result_stream(
        self,
        limit: int | None = None,
        update_timeout: Optional[float] = None,
        global_timeout: Optional[float] = None,
    ) -> AsyncGenerator[int, None]:
        """
        A generator that yields the return code of a command executed in the terminal session.
        This should never be called concurrently since it reads from the same file descriptor.

        Args:
            limit: The maximum number of return codes to yield.
            output_timeout: The maximum time to wait for new output before raising a PtyTimeoutError.
            global_timeout: The maximum total time to wait for new output before stopping the generator.
        """
        count = 0

        async for _ in _watch_for_file_updates(
            self.command_outputs_file, update_timeout, global_timeout
        ):
            for command_result in self.read_new_command_results(
                limit - count if limit else None
            ):
                yield command_result
                count += 1
                if limit and count >= limit:
                    return
        raise CommandTimeoutError()

    def read_new_output(self, size: Optional[int] = None) -> bytes:
        try:
            return self._fd.read(size)
        except ValueError as e:
            if 'read of closed file' in str(e):
                raise TerminalDeadError()
            raise

    def read_new_command_results(self, limit: Optional[int] = None) -> List[int]:
        results = []
        while True:
            try:
                data = self._commands_fd.readline()
            except ValueError as e:
                if 'read of closed file' in str(e):
                    raise TerminalDeadError()
                raise
            if not data:
                break
            results.append(int(data))
            if limit and len(results) >= limit:
                break
        return results

    def write(self, s: str):
        """Send input to the screen session."""
        escaped_input = shlex.quote(s)
        self._run_screen(f'-p 0 -X stuff {escaped_input}')

    def write_literal(self, s: str):
        """Send content to the screen, escaping caret symbols so they aren't parsed as control codes"""
        self.write(s.replace('^', r'\^'))

    def terminate(self):
        """Terminate the screen session."""
        with suppress(TerminalDeadError):
            self._run_screen('-X kill')
        self._fd.close()
        self._commands_fd.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.terminate()

    def close(self):
        """Terminate the screen session."""
        with suppress(TerminalDeadError):
            self._run_screen('-X quit')
        self._fd.close()
        self._commands_fd.close()

    def wait(self):
        """Wait for the screen session to terminate."""
        while self.isalive():
            time.sleep(0.5)

    def setwinsize(self, rows, cols):
        """Set the screen session window size."""
        self._run_screen(f'-p 0 -X height {rows} {cols}')
        self.size_file.write_text(f'{rows} {cols}')

    def getwinsize(self) -> Tuple[int, int]:
        """Get the screen session window size."""
        rows_str, cols_str = self.size_file.read_text().split()
        return int(rows_str), int(cols_str)

    def isalive(self):
        """Check if the screen session is still active."""
        result = subprocess.run(
            f'screen -list {self.screen_name}',
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if result.returncode != 0:
            return False
        matched_lines = [
            x for x in result.stdout.splitlines() if self.screen_name.encode() in x
        ]
        if len(matched_lines) != 1:
            return False
        if b'(Dead ???)' in matched_lines[0]:
            return False
        return True

    def ctrl_c(self):
        """Send a Ctrl+C to the screen session."""
        self._run_screen('-p 0 -X stuff ^C')

    def kill(self):
        """Kill the current screen session."""
        self._run_screen('-X kill')

    def _run_screen(self, cmd: str):
        result = subprocess.run(
            f'screen -S {self.screen_name} {cmd}',
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if result.returncode != 0:
            if b'No screen session found' in result.stdout:
                if b'Dead' in result.stdout:
                    self._run_screen('-wipe')
                raise TerminalDeadError()
            raise VirtualTermError(
                f'Unexpected error: {result.stdout.decode()} {result.stderr.decode()}'
            )


async def _watch_for_file_updates(
    file_path: Path,
    update_timeout: Optional[float] = None,
    global_timeout: Optional[float] = None,
) -> AsyncGenerator[None, None]:
    start_time = time.monotonic()

    with Inotify() as inotify:
        inotify.add_watch(file_path, Mask.CREATE | Mask.MODIFY)

        yield
        while True:
            timeout = update_timeout or float('inf')
            if global_timeout:
                time_remaining = global_timeout - (time.monotonic() - start_time)
                timeout = min(timeout, time_remaining)
            import math

            try:
                res = await asyncio.wait_for(
                    inotify.get(), timeout if math.isfinite(timeout) else None
                )
            except asyncio.TimeoutError:
                return
            yield
