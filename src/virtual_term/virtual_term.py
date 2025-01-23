import asyncio
import os
import pty
import shlex
import time
import uuid
from pathlib import Path
from contextlib import suppress
from typing import AsyncGenerator, List, Optional, Tuple
import select

from asyncinotify import Inotify, Mask

# If you have your own definitions for these, keep them. Otherwise, you can stub them out or adjust as needed.
from .types import (
    CommandResult,
    TerminalDeadError,
    CommandTimeoutError,
)


class AsyncPtyProcess:
    """
    Basic class for spawning and interacting with a PTY process asynchronously.
    """

    def __init__(self, cmd: List[str]):
        self.cmd = cmd
        self.reading_more_event = asyncio.Event()
        self.reader: Optional[asyncio.streams.StreamReader] = None
        self.child_pid: Optional[int] = None
        self.fd: Optional[int] = None

    async def spawn(self):
        self.child_pid, self.fd = pty.fork()
        if self.child_pid == 0:
            # In the child process; launch command through exec
            os.execvp(self.cmd[0], self.cmd)
            raise SystemExit(0)
        self.reader = self._create_stream_reader(self.fd)

    def _create_stream_reader(self, fd: int) -> asyncio.streams.StreamReader:
        stream_reader = asyncio.StreamReader()

        def reader_ready():
            try:
                data = os.read(fd, 1024)
                self.reading_more_event.set()
                if data:
                    stream_reader.feed_data(data)
                    return
            except OSError:
                pass
            stream_reader.feed_eof()

        asyncio.get_event_loop().add_reader(fd, reader_ready)
        return stream_reader

    async def write(self, data: bytes):
        """Write data to the PTY."""
        if not self.fd:
            raise TerminalDeadError('PTY file descriptor not initialized.')
        await asyncio.get_event_loop().run_in_executor(None, os.write, self.fd, data)

    def resize(self, rows: int, cols: int):
        """Resize the PTY terminal."""
        if not self.fd:
            raise TerminalDeadError('PTY file descriptor not initialized.')
        import fcntl
        import termios
        import struct

        winsize = struct.pack('HHHH', rows, cols, 0, 0)
        fcntl.ioctl(self.fd, termios.TIOCSWINSZ, winsize)

    async def stop(self):
        """Stop the PTY process and clean up."""
        if self.reader:
            self.reader.feed_eof()
        if self.fd:
            asyncio.get_event_loop().remove_reader(self.fd)
            os.close(self.fd)
        if self.child_pid:
            with suppress(ProcessLookupError):
                os.kill(self.child_pid, 9)  # Forcefully terminate the child process
        self.fd = self.reader = self.child_pid = None


async def _watch_for_file_updates(
    file_path: Path,
    update_timeout: Optional[float] = None,
    global_timeout: Optional[float] = None,
) -> AsyncGenerator[None, None]:
    """
    Watches a file for updates using inotify and yields whenever changes are detected.
    Times out if no updates are detected within update_timeout or the overall global_timeout.
    """
    start_time = time.monotonic()

    with Inotify() as inotify:
        inotify.add_watch(file_path, Mask.CREATE | Mask.MODIFY)

        yield  # Yield once initially
        while True:
            timeout = update_timeout or float('inf')
            if global_timeout:
                time_remaining = global_timeout - (time.monotonic() - start_time)
                timeout = min(timeout, time_remaining)

            import math

            if timeout <= 0:
                return

            try:
                await asyncio.wait_for(
                    inotify.get(), timeout if math.isfinite(timeout) else None
                )
            except asyncio.TimeoutError:
                return
            yield


class VirtualTerm:
    def __init__(self, id_str: str, dimensions: Tuple[int, int]):
        self.id = id_str

        from tempfile import gettempdir

        self.dimensions = dimensions  # Terminal dimensions (rows, cols)
        self.is_alive = False

        # For storing the child's exit codes
        tmp_dir = Path(gettempdir())
        self.command_outputs_file = tmp_dir / f'pty_term_{self.id}.outputs.txt'
        self.command_outputs_file.touch()
        self._commands_fd = self.command_outputs_file.open('rb')

        self.pty_process: Optional[AsyncPtyProcess] = None

    @classmethod
    async def spawn(
        cls,
        cwd: Path | None = None,
        dimensions=(24, 80),
        shell: str | None = None,
    ):
        """
        Spawn a new VirtualTerm instance, setting up a shell with a prompt that
        appends exit codes to the command_outputs_file.
        """
        instance = cls(id_str=uuid.uuid4().hex, dimensions=dimensions)

        shell_command = shell or os.environ.get('SHELL', '/bin/bash')
        # Retrieve the shell-specific prompt variable
        prompt_var = {'bash': 'PS1', 'zsh': 'PROMPT'}.get(
            os.path.basename(shell_command), 'PS1'
        )

        cd_prefix = f'cd {shlex.quote(str(cwd))}; ' if cwd else ''
        command_outputs_file = str(instance.command_outputs_file)

        # Modify the prompt so that every command's exit code is appended to command_outputs_file
        prompt_customization = (
            f'{prompt_var}="\\$(echo \\$? >> {command_outputs_file})${prompt_var}"'
        )

        # We'll place a marker in the output to know when the shell is ready.
        prefix_indicator_suffix = f'prefix:{instance.id}'
        initial_command = (
            f' {cd_prefix}{prompt_customization}; '
            f'printf "printed:""{prefix_indicator_suffix}\\n"'
        )
        prefix_indicator = f'printed:{prefix_indicator_suffix}\r\n'.encode()

        instance.pty_process = AsyncPtyProcess([shell_command])
        instance.is_alive = True
        await instance.pty_process.spawn()
        instance.pty_process.resize(dimensions[0], dimensions[1])
        await instance.pty_process.write(initial_command.encode() + b'\n')

        assert instance.pty_process.reader
        await instance.pty_process.reader.readuntil(prefix_indicator)
        return instance

    async def read_new_output(self, size: Optional[int] = None) -> bytes:
        """
        Returns new output from the PTY that we haven't returned before.
        """
        if not self.pty_process or not self.pty_process.reader:
            raise TerminalDeadError('PTY process is not active.')

        new_bytes = bytearray()
        while True:
            try:
                new_chunk = await asyncio.wait_for(
                    self.pty_process.reader.read(1024), timeout=0.001
                )
            except asyncio.TimeoutError:
                new_chunk = b''
            if not new_chunk:
                break
            new_bytes.extend(new_chunk)
        return bytes(new_bytes)

    async def read_output_stream(
        self, size: Optional[int] = None
    ) -> AsyncGenerator[bytes, None]:
        """
        A generator that yields new PTY output in chunks.
        You should not call this concurrently with read_new_output().
        """
        if not self.pty_process or not self.pty_process.reader:
            raise TerminalDeadError('PTY process is not active.')
        while self.is_alive:
            yield await self.pty_process.reader.read(1024)

    async def run_command(
        self,
        command: bytes,
        update_timeout: Optional[float] = None,
        global_timeout: Optional[float] = None,
    ) -> CommandResult:
        """
        Run a command in the terminal session, waiting for the command to finish and returning the output.
        """
        # Clear any outstanding buffer data & command results
        await self.read_new_output()

        self.read_new_command_results()

        if not self.pty_process:
            raise TerminalDeadError('PTY process is not active.')

        await self.write(command + b'\r')
        return await self.wait_for_last_command(update_timeout, global_timeout)

    async def wait_for_last_command(
        self,
        update_timeout: Optional[float] = None,
        global_timeout: Optional[float] = None,
    ) -> CommandResult:
        """
        Wait for the exit code from the last command, once the shell writes to self.command_outputs_file.
        Then return the output and the code as a CommandResult.
        """
        if not self.pty_process:
            raise TerminalDeadError('PTY process is not active.')
        async for return_code in self.read_command_result_stream(
            limit=1, update_timeout=update_timeout, global_timeout=global_timeout
        ):
            fd = self.pty_process.fd
            while True:
                self.pty_process.reading_more_event.clear()
                # Check if fd has data using select nonblocking
                r, _, _ = select.select([fd], [], [], 0)
                if not r:
                    break
                await self.pty_process.reading_more_event.wait()
            output = await self.read_new_output()
            return CommandResult(output, return_code)
        raise RuntimeError('No return code found for the last command.')

    async def read_command_result_stream(
        self,
        limit: int | None = None,
        update_timeout: Optional[float] = None,
        global_timeout: Optional[float] = None,
    ) -> AsyncGenerator[int, None]:
        """
        A generator that yields the return codes (exit codes) of commands executed in the PTY.
        Checks self.command_outputs_file for new lines (each line is an exit code).
        """
        count = 0

        async for _ in _watch_for_file_updates(
            self.command_outputs_file, update_timeout, global_timeout
        ):
            for code in self.read_new_command_results(limit - count if limit else None):
                yield code
                count += 1
                if limit and count >= limit:
                    return

        # If we end up here due to timeouts:
        raise CommandTimeoutError()

    def read_new_command_results(self, limit: Optional[int] = None) -> List[int]:
        """
        Read new exit codes from the command_outputs_file without blocking.
        """
        results = []
        while True:
            try:
                data = self._commands_fd.readline()
            except ValueError as e:
                if 'of closed file' in str(e):
                    raise TerminalDeadError()
                raise
            if not data:
                break
            data = data.strip()
            if not data:
                continue
            results.append(int(data))
            if limit and len(results) >= limit:
                break
        return results

    async def write(self, s: bytes):
        """Send input to the PTY session"""
        if not self.pty_process:
            raise TerminalDeadError('PTY process is not active.')
        await self.pty_process.write(s)

    async def terminate(self):
        """
        Terminate the PTY session.
        """
        with suppress(TerminalDeadError):
            if self.pty_process:
                await self.pty_process.stop()
        self.pty_process = None
        self._commands_fd.close()

    async def close(self):
        """
        Alias for terminate (stop/clean up PTY).
        """
        await self.terminate()

    async def wait(self):
        """
        Wait for the PTY process to exit.
        This polls using kill( child_pid, 0 ) to check if the process is still alive.
        """
        if not self.pty_process or not self.pty_process.child_pid:
            return
        while True:
            try:
                os.kill(self.pty_process.child_pid, 0)
            except OSError:
                # Process is dead
                break
            await asyncio.sleep(0.5)

    async def setwinsize(self, rows: int, cols: int):
        """
        Adjust the terminal size in the underlying PTY.
        """
        self.dimensions = (rows, cols)
        if self.pty_process:
            self.pty_process.resize(rows, cols)

    def getwinsize(self) -> Tuple[int, int]:
        """
        Return the stored terminal size.
        """
        return self.dimensions

    async def isalive(self):
        """
        Check if the PTY child process is still alive by calling os.kill(pid, 0).
        """
        if not self.pty_process or not self.pty_process.child_pid:
            return False
        try:
            os.kill(self.pty_process.child_pid, 0)
            return True
        except OSError:
            return False

    async def ctrl_c(self):
        """
        Send a Ctrl+C to the PTY session.
        """
        await self.write(b'\x03')

    async def kill(self):
        """
        Kill the PTY session.
        """
        if self.pty_process and self.pty_process.child_pid:
            try:
                os.kill(self.pty_process.child_pid, 9)
            except OSError:
                pass
