import asyncio
from math import pi
import os
import pty
import shlex
import time
import uuid
from pathlib import Path
from contextlib import suppress
from typing import AsyncGenerator, Awaitable, Callable, List, Optional, Tuple

from asyncinotify import Inotify, Mask

# If you have your own definitions for these, keep them. Otherwise, you can stub them out or adjust as needed.
from .types import (
    CommandResult,
    VirtualTermError,
    TerminalDeadError,
    CommandTimeoutError,
)

class AsyncPtyProcess:
    """
    Basic class for spawning and interacting with a PTY process asynchronously.
    """

    def __init__(self, cmd: List[str]):
        self.cmd = cmd
        self.fd: Optional[int] = None
        self.child_pid: Optional[int] = None
        self.flush_event = asyncio.Event()

    async def spawn(self):
        """Spawn the PTY process."""
        loop = asyncio.get_event_loop()
        self.child_pid, self.fd = pty.fork()

        if self.child_pid == 0:
            # In the child process
            os.execvp(self.cmd[0], self.cmd)
        else:
            # In the parent process, simply return
            return self

    async def read_output(self) -> AsyncGenerator[bytes, None]:
        """
        Asynchronously read output from the PTY.
        Yields data (decoded) in chunks as it arrives.
        """
        if self.fd is None:
            raise RuntimeError("PTY process not spawned")

        fd = self.fd
        loop = asyncio.get_event_loop()
        stream_reader = asyncio.StreamReader()

        def reader_ready():
            try:
                print('READER READY...')
                data = os.read(fd, 1024)
                print('GET ', len(data))
                if data:
                    stream_reader.feed_data(data)
                else:
                    stream_reader.feed_eof()
            except OSError:
                stream_reader.feed_eof()

        loop.add_reader(fd, reader_ready)

        try:
            while True:
                was_set = self.flush_event.is_set()
                if not was_set and stream_reader.at_eof():
                    self.flush_event.set()
                print('Check', was_set, stream_reader.at_eof())
                chunk = await stream_reader.read(1024)
                print('Len:', len(chunk))
                if not chunk:
                    break
                yield chunk
                if not was_set:
                    self.flush_event.set()
        finally:
            loop.remove_reader(fd)

    async def write(self, data: str):
        """Write data to the PTY."""
        if self.fd is None:
            raise RuntimeError("PTY process not spawned")
        await asyncio.get_event_loop().run_in_executor(None, os.write, self.fd, data.encode())

    def resize(self, rows: int, cols: int):
        """Resize the PTY terminal."""
        if self.fd is None:
            raise RuntimeError("PTY process not spawned")
        import fcntl
        import termios
        import struct

        winsize = struct.pack("HHHH", rows, cols, 0, 0)
        fcntl.ioctl(self.fd, termios.TIOCSWINSZ, winsize)

    async def stop(self):
        """Stop the PTY process and clean up."""
        if self.fd:
            os.close(self.fd)
            self.fd = None
        if self.child_pid:
            try:
                os.kill(self.child_pid, 9)  # Forcefully terminate the child process
            except ProcessLookupError:
                pass
            self.child_pid = None


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
            timeout = update_timeout or float("inf")
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

        self.log_buffer = bytearray()        # Stores raw PTY output
        self.new_content_event = asyncio.Event()  # Event to signal new content in log_buffer
        self.log_offset = 0                  # Track how much of log_buffer has been read
        self.dimensions = dimensions         # Terminal dimensions (rows, cols)
        self.is_alive = False

        # For storing the child's exit codes
        tmp_dir = Path(gettempdir())
        self.command_outputs_file = tmp_dir / f"pty_term_{self.id}.outputs.txt"
        self.command_outputs_file.touch()
        self._commands_fd = self.command_outputs_file.open("rb")

        # Initial references to our PTY process
        self.pty_process: Optional[AsyncPtyProcess] = None
        self._reader_task: Optional[asyncio.Task] = None

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

        shell_command = shell or os.environ.get("SHELL", "/bin/bash")
        # Retrieve the shell-specific prompt variable
        prompt_var = {"bash": "PS1", "zsh": "PROMPT"}.get(os.path.basename(shell_command), "PS1")

        cd_prefix = f"cd {shlex.quote(str(cwd))}; " if cwd else ""
        command_outputs_file = str(instance.command_outputs_file)

        # Modify the prompt so that every command's exit code is appended to command_outputs_file
        prompt_customization = (
            f'{prompt_var}="\\$(echo \\$? >> {command_outputs_file})${prompt_var}"'
        )

        # We'll place a marker in the output to know when the shell is ready.
        prefix_indicator = f"prefix:{instance.id}"
        initial_command = (
            f" {cd_prefix}{prompt_customization}; "
            f'echo "printed:""{prefix_indicator}"'
        )

        # Spawn the PTY process
        instance.pty_process = AsyncPtyProcess([shell_command])
        instance.is_alive = True
        await instance.pty_process.spawn()
        # Resize the PTY to requested dimensions
        # instance.pty_process.resize(dimensions[0], dimensions[1])

        # Start reading from the PTY in the background
        instance._reader_task = asyncio.create_task(instance._capture_pty_output())

        # Send our initial command to set up prompt logic
        await instance.pty_process.write(initial_command + "\n")

        await instance.wait_for_last_command()

        # Wait until we see the prefix_indicator in the PTY output or time out
        prefix_found = False
        for _ in range(50):
            if instance.log_buffer.find(f"printed:{prefix_indicator}".encode()) != -1:
                prefix_found = True
                break
            await asyncio.sleep(0.1)

        if not prefix_found:
            raise VirtualTermError(f"Prefix indicator not found in PTY output.")
        print('SAW PREFIX:', instance.log_buffer)

        # Mark that we've handled everything up to and including the prefix
        idx = instance.log_buffer.rfind(f"printed:{prefix_indicator}".encode())
        idx += len(f"printed:{prefix_indicator}")
        # Skip any trailing newlines
        while idx < len(instance.log_buffer) and instance.log_buffer[idx] in b"\r\n":
            idx += 1
        instance.log_offset = idx

        return instance

    async def _capture_pty_output(self):
        """
        Continuously read chunks from the pty_process and store them in self.log_buffer.
        """
        if not self.pty_process:
            return
        try:
            async for chunk in self.pty_process.read_output():
                self.log_buffer.extend(chunk)
                self.new_content_event.set()
        except Exception as e:
            raise
            print('Exception capturing pty output:', repr(e))
        self.is_alive = False
        self.new_content_event.set()

    def read_new_output(self, size: Optional[int] = None) -> bytes:
        """
        Returns new output from the PTY that we haven't returned before.
        """
        if self.log_offset >= len(self.log_buffer):
            return b""

        end_idx = len(self.log_buffer)
        if size is not None:
            end_idx = min(self.log_offset + size, len(self.log_buffer))

        new_data = self.log_buffer[self.log_offset:end_idx]
        self.log_offset = end_idx
        return bytes(new_data)

    async def read_output_stream(
        self,
        size: Optional[int] = None
    ) -> AsyncGenerator[bytes, None]:
        """
        A generator that yields new PTY output in chunks.
        You should not call this concurrently with read_new_output().
        """
        while self.is_alive:
            chunk = self.read_new_output(size)
            if chunk:
                yield chunk
            else:
                await self.new_content_event.wait()
                self.new_content_event.clear()

    async def run_command(
        self,
        command: str,
        update_timeout: Optional[float] = None,
        global_timeout: Optional[float] = None,
    ) -> CommandResult:
        """
        Run a command in the terminal session, waiting for the command to finish and returning the output.
        """
        # Clear any outstanding buffer data & command results
        self.read_new_output()
        self.read_new_command_results()

        if not self.pty_process:
            raise TerminalDeadError("PTY process is not active.")

        await self.write(command + "\r")
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
        assert self.pty_process
        async for return_code in self.read_command_result_stream(
            limit=1, update_timeout=update_timeout, global_timeout=global_timeout
        ):
            self.pty_process.flush_event.clear()
            await self.pty_process.flush_event.wait()
            output = self.read_new_output()
            return CommandResult(output, return_code)
        raise RuntimeError("No return code found for the last command.")

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
                if "read of closed file" in str(e):
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

    async def write(self, s: str):
        """Send input to the PTY session"""
        if not self.pty_process:
            raise TerminalDeadError("PTY process is not active.")
        await self.pty_process.write(s)

    async def write_literal(self, s: str):
        """
        Send content to the PTY, escaping '^' so they aren't interpreted as control codes.
        """
        await self.write(s.replace("^", r"\^"))

    async def terminate(self):
        """
        Terminate the PTY session.
        """
        if self._reader_task:
            self._reader_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._reader_task

        with suppress(TerminalDeadError):
            if self.pty_process:
                await self.pty_process.stop()

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
        # ASCII 3 = Ctrl+C
        await self.write("\x03")

    async def kill(self):
        """
        Kill the PTY session.
        """
        if self.pty_process and self.pty_process.child_pid:
            try:
                os.kill(self.pty_process.child_pid, 9)
            except OSError:
                pass
