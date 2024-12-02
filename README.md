# VirtualTerm

_A simple Python library for creating and interacting with virtual terminals_

VirtualTerm is a Python library designed for creating and managing virtual terminal sessions programmatically. It allows users to run interactive shell commands, handle command timeouts, and monitor output streams within isolated terminal environments. This library is particularly useful for developers building automation tools, testing environments, or managing interactive shell-based workflows.

```python
import asyncio
from virtual_term import VirtualTerm

async def main():
    with await VirtualTerm.spawn() as term:
        term.write("sudo apt-get upgrade\n")
        await asyncio.sleep(1)
        print(term.read_new_output().decode())

        term.write("y\n")  # Respond with 'y' to confirm upgrade
        result = await term.wait_for_last_command()

        print(result.output)
        assert result.return_code == 0

asyncio.run(main())
```

## Installation

Install with pip:

```sh
pip install virtual-term
```

## Development

VirtualTerm uses Rye for dependency management and development workflows. To get started with development, ensure you have [Rye](https://github.com/astral-sh/rye) installed, then clone the repository and set up the environment:

```sh
git clone https://github.com/MatthewScholefield/virtual-term.git
cd virtual-term
rye sync
rye run pre-commit install

# Run tests
rye test
```
